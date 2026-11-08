"""Routes for the AG Studio "Impact studio": scoped data sources and the Studio agent endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..ai import insights_agent
from ..config import settings
from ..core import insights
from . import auth

router = APIRouter()


def _scope(request: Request, scope: str) -> tuple[dict, dict]:
    if scope == "admin":
        user = auth.require(request, "admin")
        return user, insights.admin_sources()
    if scope != "sponsor":
        raise HTTPException(status_code=400, detail="unknown scope")
    user = auth.require(request, "sponsor")
    return user, insights.sponsor_sources(user)


@router.get("/api/insights/data")
async def insights_data(request: Request, scope: str = "sponsor"):
    """Studio data sources for the signed-in user. A sponsor only ever receives their own rows."""
    _, data = _scope(request, scope)
    return data


@router.post("/api/insights/agent")
async def insights_agent_round(request: Request, scope: str = "sponsor"):
    """One round of Studio's client tool runner: AG-UI messages and Studio tools in, AG-UI events out."""
    auth.check_csrf(request, request.headers.get("x-csrf"))
    _, data = _scope(request, scope)
    messages, tools = insights_agent.parse_request(await request.json())
    events = await insights_agent.respond(messages, tools, insights.schema_summary(data))
    return {"events": events}


def studio_license_key() -> str:
    """Rendered into the insights pages at runtime; AG Studio keys are used client-side by design."""
    return settings.ag_studio_license_key
