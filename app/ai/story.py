"""The short, dignified note on a bill card, written from the poster's own words with consent."""

from __future__ import annotations

from . import llm
from .intake import CATEGORY_LABELS
from .redact import scrub

SYSTEM = """You write the short note shown on a bill card in an app where sponsors pay household bills directly
to the biller. Write from the poster's own words. Rules:
- Two sentences, at most 260 characters in total.
- Third person, warm, plain and dignified. No pity, no drama, no exclamation marks, no emojis.
- Never include personal names (except the alias given), phone numbers, account numbers, addresses, the name of a
  school, hospital or employer, or any detail not in the poster's words.
- Do not promise outcomes. Do not ask for money. Do not mention the app.
Return JSON: {"story": "..."}"""


def template(category: str, city: str, alias: str, words: str) -> str:
    who = alias or "A household"
    where = f" in {city}" if city else ""
    label = CATEGORY_LABELS.get(category, "household").lower()
    base = f"{who}{where} is asking for help with a {label} bill."
    if words:
        first = words.strip().split(".")[0][:150]
        base += f" In their words: \"{first}.\""
    return base


async def write(*, category: str, city: str, country: str, alias: str, words: str,
                names: list[str] | None = None, address: str | None = None,
                accounts: list[str] | None = None) -> tuple[str, str]:
    """Returns (story, source) where source is 'ai' or 'template'. Always scrubbed, including the bill's own account
    reference however the poster typed it."""
    if alias:  # the poster chose to show this first name, so it is not redacted
        keep = alias.lower()
        names = [" ".join(p for p in n.split() if p.lower() != keep) for n in (names or [])]
    extra = [address] if address else None
    words = scrub(words or "", names=names, extra=extra, accounts=accounts)
    if words.strip():
        data = await llm.chat_json(SYSTEM, f"Category: {CATEGORY_LABELS.get(category, category)}\nCity: {city}\n"
                                   f"Country: {country}\nAlias: {alias or 'none'}\nPoster's words: {words[:1200]}",
                                   kind="story", max_tokens=600)
        if data and isinstance(data.get("story"), str) and 20 < len(data["story"]) <= 400:
            return scrub(data["story"].strip(), names=names, extra=extra, accounts=accounts), "ai"
    return scrub(template(category, city, alias, words), names=names, extra=extra, accounts=accounts), "template"


THANKS_SYSTEM = """Lightly polish a thank-you note from a person whose bill was paid by strangers. Keep their voice
and meaning, fix only spelling and grammar, at most 240 characters, no names, no numbers, no emojis.
Return JSON: {"text": "..."}"""


async def polish_thanks(text: str, names: list[str] | None = None, extra: list[str] | None = None,
                        accounts: list[str] | None = None) -> str:
    text = scrub(text, names=names, extra=extra, accounts=accounts)[:400]
    data = await llm.chat_json(THANKS_SYSTEM, text, kind="thanks", max_tokens=400)
    if data and isinstance(data.get("text"), str) and data["text"].strip():
        return scrub(data["text"].strip(), names=names, extra=extra, accounts=accounts)[:260]
    return text[:260]
