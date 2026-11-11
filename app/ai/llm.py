"""Thin client for open models on Groq's OpenAI-compatible endpoint.

Every caller has a non-AI fallback; these functions return None on any failure so callers can
fall back without special cases. The API key is never logged.
"""

from __future__ import annotations

import base64
import contextvars
import json
import logging
import re

import httpx

from ..config import settings

URL = "https://api.groq.com/openai/v1/chat/completions"
log = logging.getLogger("billkind.ai")

# Set for one request when its account is over the model-call limit (app/web/limits.py): every caller then takes its
# usual non-AI fallback, exactly as when the model is unavailable.
_blocked: contextvars.ContextVar[bool] = contextvars.ContextVar("ai_blocked", default=False)


def block_for_this_request() -> None:
    _blocked.set(True)


def available() -> bool:
    return settings.ai_enabled and not _blocked.get()


# A record of recent calls for the admin "AI activity" panel (no prompts with personal data).
CALLS: list[dict] = []


def _record(kind: str, model: str, ok: bool, ms: int, note: str = "") -> None:
    CALLS.append({"kind": kind, "model": model, "ok": ok, "ms": ms, "note": note[:160]})
    del CALLS[:-50]


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


async def chat(messages: list[dict], *, model: str | None = None, json_mode: bool = False,
               max_tokens: int = 900, temperature: float = 0.2, kind: str = "chat",
               timeout: float = 45) -> str | None:
    if not available():
        return None
    model = model or settings.ai_text_model
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
        body["max_tokens"] = max_tokens + 1200  # reasoning tokens count against the limit
    import time
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as h:
            r = await h.post(URL, json=body, headers={"Authorization": f"Bearer {settings.groq_api_key}"})
        ok = r.status_code == 200
        _record(kind, model, ok, int((time.time() - t0) * 1000))
        if not ok and json_mode and "json_validate_failed" in r.text:
            body.pop("response_format", None)  # retry once and parse the JSON ourselves
            async with httpx.AsyncClient(timeout=45) as h:
                r = await h.post(URL, json=body, headers={"Authorization": f"Bearer {settings.groq_api_key}"})
            ok = r.status_code == 200
        if not ok:
            log.warning("AI call %s failed with HTTP %s", kind, r.status_code)
            return None
        return r.json()["choices"][0]["message"].get("content") or None
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        _record(kind, model, False, int((time.time() - t0) * 1000))
        log.warning("AI call %s failed: %s", kind, type(exc).__name__)
        return None


async def chat_raw(messages: list[dict], *, tools: list[dict] | None = None, kind: str = "tools",
                   max_tokens: int = 700, tool_choice: str | None = None,
                   models: list[str] | None = None) -> dict | None:
    """One chat turn with optional tool definitions. Returns the assistant message dict or None.

    `models` is an ordered fallback list: on a rate limit (HTTP 429) or a server error the next model is tried.
    """
    if not available():
        return None
    import asyncio
    import time
    for attempt, model in enumerate(models or [settings.ai_text_model]):
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens + 1200, "temperature": 0.1}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=60) as h:
                r = await h.post(URL, json=body, headers={"Authorization": f"Bearer {settings.groq_api_key}"})
        except httpx.HTTPError as exc:
            _record(kind, model, False, int((time.time() - t0) * 1000))
            log.warning("AI call %s on %s failed: %s", kind, model, type(exc).__name__)
            continue
        note = ""
        if r.status_code == 200:
            try:
                u = r.json().get("usage") or {}
                note = f"{u.get('prompt_tokens', '?')} in, {u.get('completion_tokens', '?')} out"
            except ValueError:
                pass
        else:
            try:
                note = f"HTTP {r.status_code}: " + str(r.json().get("error", {}).get("message", ""))[:140]
            except ValueError:
                note = f"HTTP {r.status_code}"
        _record(kind, model, r.status_code == 200, int((time.time() - t0) * 1000), note)
        if r.status_code == 200:
            try:
                return r.json()["choices"][0]["message"]
            except (KeyError, ValueError):
                return None
        log.warning("AI call %s on %s failed with HTTP %s", kind, model, r.status_code)
        if r.status_code == 429 and attempt == 0 and len(models or []) <= 1:
            await asyncio.sleep(min(float(r.headers.get("retry-after", "2") or 2), 6))
            continue
        if r.status_code not in (400, 413, 429, 500, 502, 503, 504):
            return None
    return None


async def chat_json(system: str, user: str, *, kind: str, max_tokens: int = 900) -> dict | None:
    text = await chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                      json_mode=True, max_tokens=max_tokens, kind=kind)
    return _parse_json(text) if text else None


async def vision_json(prompt: str, image: bytes, mime: str, *, kind: str = "vision") -> dict | None:
    b64 = base64.b64encode(image).decode()
    text = await chat([{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]}], model=settings.ai_vision_model, max_tokens=900, kind=kind, timeout=14)
    return _parse_json(text) if text else None
