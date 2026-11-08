"""Server side of the AG Studio "Ask your impact" agent.

AG Studio's Studio Agent Framework runs a `clientToolRunner` in the browser. Each round it posts the AG-UI message
history and the Studio tools it is willing to execute (name, description, JSON-schema parameters) to
/api/insights/agent. This module asks an open model on Groq for the next step and answers with AG-UI events:
text deltas and tool calls. Studio executes the tool calls in the browser against the data sources the server
already scoped to the signed-in user, appends the results, and calls again until the model stops calling tools.

The Groq key never leaves the server. The model sees only a schema summary of the user's own scoped data plus
whatever Studio tool results come back, and its final text is checked so it cannot state figures it did not get
from a query result.
"""

from __future__ import annotations

import re
import uuid

from . import llm

MAX_ROUNDS = 12
MAX_TOOLS = 24
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,48}$")
# Studio's built-in tool vocabulary plus the harness's own delegation tools.
ALLOWED_TOOLS = {
    "view_schema", "view_page", "view_widget", "execute_query", "add_widget", "configure_widget", "position_widget",
    "remove_widget", "add_page_filter", "remove_page_filter", "add_widget_filter", "remove_widget_filter",
    "add_page", "rename_page", "delegate_to", "complete_task",
}

SYSTEM = """You are the analyst inside Billkind's Impact studio, an AG Studio dashboard. Billkind lets sponsors pay
household bills (school fees, medical, electricity, water, rent, phone) directly to verified billers, never to the
person who posted the bill.

You build and adjust dashboard widgets with the Studio tools you are given. Work like this:
1. Use view_schema (or the schema below) to find the right table and fields.
2. Use execute_query to check the numbers before you describe them.
3. Use add_widget, then configure_widget, to build what the user asked for. Prefer clear titles in plain words.
4. Finish with one or two short sentences saying what you built.

Rules:
- The data contains only what this user is allowed to see. Do not speculate about anyone else's data.
- Never state a number you did not get from an execute_query result in this conversation.
- No personal names, account numbers or contact details exist in the data; do not invent any.
- Write plainly. No emojis, no exclamation marks.

Data available:
{schema}"""


def _ag_ui_to_openai(messages: list[dict]) -> list[dict]:
    """Convert AG-UI messages to the OpenAI chat format Groq expects. Client 'system' messages are dropped."""
    out: list[dict] = []
    for m in messages[-40:]:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):  # AG-UI allows content parts; keep the text ones
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = (content or "")[:6000] if isinstance(content, str) else ""
        if role == "user":
            out.append({"role": "user", "content": content[:2000]})
        elif role == "assistant":
            msg: dict = {"role": "assistant", "content": content}
            calls = []
            for tc in (m.get("toolCalls") or [])[:8]:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                if TOOL_NAME.match(name or ""):
                    calls.append({"id": str(tc.get("id", ""))[:64] or f"call_{uuid.uuid4().hex[:8]}",
                                  "type": "function",
                                  "function": {"name": name, "arguments": str(fn.get("arguments") or "{}")[:8000]}})
            if calls:
                msg["tool_calls"] = calls
            if content or calls:
                out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": str(m.get("toolCallId", ""))[:64], "content": content})
    # Drop tool results whose call is not in the kept history (OpenAI format rejects orphans).
    known = {c["id"] for m in out if m["role"] == "assistant" for c in m.get("tool_calls", [])}
    return [m for m in out if m["role"] != "tool" or m["tool_call_id"] in known]


def _clean_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in (tools or [])[:MAX_TOOLS]:
        name = str(t.get("name", ""))
        if name not in ALLOWED_TOOLS or not TOOL_NAME.match(name):
            continue
        params = t.get("parameters") if isinstance(t.get("parameters"), dict) else {"type": "object", "properties": {}}
        out.append({"type": "function", "function": {"name": name, "description": str(t.get("description", ""))[:1000],
                                                     "parameters": params}})
    return out


def _text_events(text: str) -> list[dict]:
    mid = f"msg-{uuid.uuid4().hex[:10]}"
    events = [{"type": "TEXT_MESSAGE_START", "messageId": mid, "role": "assistant"}]
    for i in range(0, len(text), 60):  # small deltas so the panel streams
        events.append({"type": "TEXT_MESSAGE_CONTENT", "messageId": mid, "delta": text[i:i + 60]})
    events.append({"type": "TEXT_MESSAGE_END", "messageId": mid})
    return events


def _tool_events(call: dict) -> list[dict]:
    cid = call.get("id") or f"call_{uuid.uuid4().hex[:10]}"
    fn = call.get("function") or {}
    return [{"type": "TOOL_CALL_START", "toolCallId": cid, "toolCallName": fn.get("name", "")},
            {"type": "TOOL_CALL_ARGS", "toolCallId": cid, "delta": fn.get("arguments") or "{}"},
            {"type": "TOOL_CALL_END", "toolCallId": cid}]


NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


def ungrounded_numbers(text: str, evidence: str) -> list[str]:
    """Numbers in the answer (two or more digits, or with a decimal point) that no tool result contains."""
    ev = evidence.replace(",", "")
    missing = []
    for m in NUMBER.finditer(text):
        tok = m.group(0).replace(",", "")
        if len(tok.replace(".", "")) < 2 and "." not in tok:
            continue  # small counts such as "3 widgets" are fine
        if tok not in ev and tok.rstrip("0").rstrip(".") not in ev:
            missing.append(tok)
    return missing


async def respond(messages: list[dict], tools: list[dict], schema: str) -> list[dict]:
    """One round. Returns AG-UI content events (Studio adds RUN_STARTED and RUN_FINISHED itself)."""
    convo = _ag_ui_to_openai(messages)
    tool_defs = _clean_tools(tools)
    rounds = sum(1 for m in convo if m["role"] == "tool")
    if rounds >= MAX_ROUNDS:
        tool_defs = []  # force a final answer
    if not convo or convo[-1]["role"] not in ("user", "tool"):
        return _text_events("Ask me for a widget, for example: money given by country, or bills paid by month.")
    sys_msg = {"role": "system", "content": SYSTEM.format(schema=schema[:6000])}
    reply = await llm.chat_raw([sys_msg, *convo], tools=tool_defs or None, kind="insights-agent", max_tokens=900)
    if reply is None:
        return _text_events("The analyst is offline right now. You can still build widgets yourself: switch to edit "
                            "mode and drag fields from the data panel.")
    events: list[dict] = []
    calls = [c for c in (reply.get("tool_calls") or [])
             if (c.get("function") or {}).get("name") in {t["function"]["name"] for t in tool_defs}]
    text = (reply.get("content") or "").strip()
    for a, b in (("—", ", "), ("–", "-"), ("‑", "-"), ("**", ""), ("`", "")):
        text = text.replace(a, b)
    if text and not calls:
        evidence = " ".join(m["content"] for m in convo if m["role"] == "tool")
        if ungrounded_numbers(text, evidence):
            text = "Done. The figures are in the widget on the dashboard."
    if text:
        events.extend(_text_events(text))
    for c in calls[:6]:
        events.extend(_tool_events(c))
    if not events:
        events = _text_events("Done.")
    return events


def parse_request(body: dict) -> tuple[list[dict], list[dict]]:
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    return [m for m in messages if isinstance(m, dict)], [t for t in tools if isinstance(t, dict)]

