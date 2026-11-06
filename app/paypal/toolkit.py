"""PayPal's official Agent Toolkit (Python), used for the support assistant's live order lookups.

The toolkit's shared core (paypal_agent_toolkit.shared) needs only requests and pydantic, so it is installed
without its LangChain, CrewAI and OpenAI Agents extras. Only read tools are enabled here; payments go through
Billkind's own capped code paths.
"""

from __future__ import annotations

import json

from ..config import settings

ALLOWED = {"get_order_details", "list_transactions"}
_api = None


def api():
    global _api
    if _api is None:
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Context
        _api = PayPalAPI(client_id=settings.paypal_client_id or "fake-client",
                         secret=settings.paypal_client_secret or "fake-secret",
                         context=Context(sandbox=True, source="BILLKIND"))
        # Point the toolkit at the same PayPal base URL as the rest of the app (the sandbox, or the local fake).
        _api._paypal_client.base_url = settings.paypal_base_url
    return _api


def call(method: str, params: dict) -> dict:
    if method not in ALLOWED:
        raise PermissionError(f"{method} is not enabled for the assistant")
    out = api().run(method, params)
    return json.loads(out) if isinstance(out, str) else out


def get_order_details(order_id: str) -> dict:
    return call("get_order_details", {"order_id": order_id})
