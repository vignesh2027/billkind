"""Thin client for open models on Groq's OpenAI-compatible endpoint.

Every caller has a non-AI fallback; these functions return None on any failure so callers can
fall back without special cases. The API key is never logged.
"""

from __future__ import annotations

import base64
import json
import logging
import re

import httpx

from ..config import settings

URL = "https://api.groq.com/openai/v1/chat/completions"
log = logging.getLogger("billkind.ai")

# A record of recent calls for the admin "AI activity" panel (no prompts with personal data).
CALLS: list[dict] = []


def _record(kind: str, model: str, ok: bool, ms: int) -> None:
    CALLS.append({"kind": kind, "model": model, "ok": ok, "ms": ms})
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
               max_tokens: int = 900, temperature: float = 0.2, kind: str = "chat") -> str | None:
    if not settings.ai_enabled:
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
        async with httpx.AsyncClient(timeout=45) as h:
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


async def chat_json(system: str, user: str, *, kind: str, max_tokens: int = 900) -> dict | None:
    text = await chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                      json_mode=True, max_tokens=max_tokens, kind=kind)
    return _parse_json(text) if text else None


async def vision_json(prompt: str, image: bytes, mime: str, *, kind: str = "vision") -> dict | None:
    b64 = base64.b64encode(image).decode()
    text = await chat([{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]}], model=settings.ai_vision_model, max_tokens=900, kind=kind)
    return _parse_json(text) if text else None
