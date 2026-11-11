"""Small in-process rate limits (one uvicorn worker, so in-memory state is enough).

Keys are user ids where a user is signed in, and the client address otherwise. Behind Caddy the address uvicorn
sees is the proxy's, so a private or loopback peer is trusted for X-Forwarded-For (the right-most entry, the one
the proxy itself added); a public peer is used as is and its header ignored.
"""

from __future__ import annotations

import ipaddress
import time
from collections import deque

from fastapi import Request


class Window:
    """At most `limit` hits per key in any `seconds` long window."""

    MAX_KEYS = 20_000

    def __init__(self, limit: int, seconds: float):
        self.limit, self.seconds = limit, seconds
        self.hits: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self.hits.setdefault(key, deque())
        while q and q[0] <= now - self.seconds:
            q.popleft()
        return q

    def allow(self, key: str, now: float | None = None) -> bool:
        """Record a hit and say whether it is within the limit (hits over the limit are not recorded)."""
        now = now or time.time()
        if len(self.hits) > self.MAX_KEYS:
            self.hits = {k: q for k, q in self.hits.items() if q and q[-1] > now - self.seconds}
        q = self._recent(key, now)
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def over(self, key: str, now: float | None = None) -> bool:
        """Whether the key is at its limit, without recording a hit."""
        return len(self._recent(key, now or time.time())) >= self.limit

    def reset(self) -> None:
        self.hits.clear()


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    try:
        private = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        private = False
    if private:
        fwd = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if fwd:
            return fwd[-1]
    return peer


# Model calls from one account: help chat, Impact studio, bulk filters, agent rules, thank-you polish. Over the
# limit the request still works, on the same non-AI fallback used when the model is unavailable.
AI_CALLS = Window(30, 600)
# Failed sign-ins from one address.
LOGIN_FAILURES = Window(20, 600)
# New per-browser demo poster accounts from one address; past it, that address's latest one is reused.
DEMO_POSTERS = Window(20, 3600)
# PayPal orders one sponsor account can open.
ORDERS = Window(30, 600)

# Freshly rendered sample bills from one address (each costs about half a second of CPU).
SAMPLES = Window(30, 60)

ALL = (AI_CALLS, LOGIN_FAILURES, DEMO_POSTERS, ORDERS, SAMPLES)
