"""Request-level guards, written as plain ASGI middleware so they also work around streamed responses (SSE).

BodyLimit refuses request bodies over a per-path limit before the app reads them. Without it, FastAPI parses an
upload form (spooling file parts to disk) before the route even checks who is signed in, and the webhook body is
read into memory whole.

SecurityHeaders stops other sites framing Billkind pages (clickjacking the one-click buttons), and adds the usual
nosniff, referrer and, on https, HSTS headers. It sets no script policy: the pages use inline scripts and PayPal's.
"""

from __future__ import annotations

from typing import Callable

from starlette.exceptions import HTTPException

KB = 1024


class BodyLimit:
    def __init__(self, app, limit_for: Callable[[str], int]):
        self.app = app
        self.limit_for = limit_for

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["path"])
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None:
            try:
                too_big = int(length) > limit
            except ValueError:
                too_big = True
            if too_big:
                await _too_large(send)
                return
        seen = 0

        async def counted():  # bodies sent without a length (chunked) are counted as they arrive
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise HTTPException(status_code=413, detail="That request is too large.")
            return message

        await self.app(scope, counted, send)


async def _too_large(send) -> None:
    body = b'{"detail":"That request is too large."}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class SecurityHeaders:
    def __init__(self, app, https: bool):
        self.app = app
        self.headers = [(b"x-frame-options", b"DENY"), (b"content-security-policy", b"frame-ancestors 'none'"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"strict-origin-when-cross-origin")]
        if https:
            self.headers.append((b"strict-transport-security", b"max-age=31536000"))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                have = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in self.headers if k not in have]
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, with_headers)
