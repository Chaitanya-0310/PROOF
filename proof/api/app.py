"""
The operator console API.

A thin FastAPI layer over the machinery already built. It adds no business
logic of its own -- the dashboard reads through the same domain tools, the
approvals go through the same `decide()` the CLI uses, and Ask runs the same
coordinator. The web tier is a view, not a second brain.

One principle carried over from the agent design: **identity comes from the
transport, not the request body.** The acting principal is read from the
`X-Proof-Principal` header (standing in for a session cookie / OIDC subject),
never from JSON a caller can shape. So a chat message that says "I am the
regional director" changes nothing here either -- the header is the only
identity the server trusts.

Endpoints that need no model (dashboard, approvals, identity) work today.
`/api/ask` runs the agent and returns a clear "model not connected" state
until an LLM key is configured.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from proof.db import load_env
from proof.identity import DEMO_PRINCIPALS, NoPrincipal, Principal, resolve

load_env()

app = FastAPI(title="PROOF operator console", version="0.5")

# The React dev server (Vite) runs on a different origin, so the browser needs
# CORS to reach the API. Kept to localhost -- this is a dev/demo console.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def current_principal(x_proof_principal: str | None = Header(default=None)) -> Principal:
    """Resolve the acting principal from the transport header.

    Defaults to the shift supervisor so the demo works with no header set.
    In a real deployment this validates a session token instead.
    """
    try:
        return resolve(x_proof_principal)
    except NoPrincipal as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


# =====================================================================
# Identity
# =====================================================================
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "proof-console"}


@app.get("/api/principals")
def principals() -> list[dict]:
    """The demo identities the UI's 'acting as' switcher offers."""
    return [{
        "principal_id": p.principal_id,
        "display_name": p.display_name,
        "role": p.role,
        "plant_scope": list(p.plant_scope) or ["(all plants)"],
        "capabilities": sorted(p.capabilities),
    } for p in DEMO_PRINCIPALS.values()]


@app.get("/api/whoami")
def whoami(p: Principal = Depends(current_principal)) -> dict:
    return {
        "principal_id": p.principal_id,
        "display_name": p.display_name,
        "role": p.role,
        "plant_scope": list(p.plant_scope) or ["(all plants)"],
        "capabilities": sorted(p.capabilities),
    }


def _default_plant(p: Principal, plant: str | None) -> str:
    """Pick the plant to show: the requested one, else the principal's own."""
    if plant:
        return plant
    return p.plant_scope[0] if p.plant_scope else "TOR1"


# =====================================================================
# Plant dashboard (read-only, no model)
# =====================================================================
@app.get("/api/dashboard")
def dashboard(plant: str | None = None,
              p: Principal = Depends(current_principal)) -> dict:
    """One read-only snapshot for the dashboard view.

    Assembled from the same domain tools the agents use, so what the operator
    sees and what the agent reasons over can never disagree.
    """
    from proof.tools import demand, inventory, production

    plant_code = _default_plant(p, plant)

    # A scoped principal is not shown another plant's floor, mirroring the
    # authz boundary rather than relying on the UI to hide it.
    if not p.in_scope(plant_code):
        raise HTTPException(
            status_code=403,
            detail=f"{p.principal_id} ({p.role}) is not scoped to {plant_code}")

    down = production.get_open_downtime(plant_code)["rows"]

    # For each stopped line, run the scrap clock at the operator's ETA.
    stopped = []
    for ev in down:
        wip = inventory.project_wip_expiry(
            ev["line_id"], ev["operator_eta_minutes"] or 0)["rows"]
        loss = production.estimate_output_loss(
            ev["line_id"], ev["operator_eta_minutes"] or 0)["rows"]
        lost_units = loss[0]["units_not_produced"] if loss else 0
        scrap_units = sum(b["units"] for b in wip if b["expires_before_restart"])
        stopped.append({
            "plant": ev["plant_code"], "line": ev["line_code"],
            "line_id": ev["line_id"], "run_id": ev["run_id"],
            "reason": ev["reason_code"], "detail": ev["reason_detail"],
            "minutes_down": ev["minutes_down_so_far"],
            "eta_minutes": ev["operator_eta_minutes"],
            "throughput_loss_units": lost_units,
            "wip_scrap_units": scrap_units,
            "total_at_risk_units": lost_units + scrap_units,
            "wip_batches": wip,
        })

    at_risk = demand.get_at_risk_orders(plant_code, 24)["rows"]
    scrap = production.get_scrap_breakdown(30, plant_code, "line")["rows"]

    from proof.tools import actions as action_tools
    pending = action_tools.list_pending_actions(plant_code)["rows"]

    return {
        "plant": plant_code,
        "generated_for": p.principal_id,
        "open_downtime": stopped,
        "at_risk_orders": at_risk,
        "scrap_by_line": scrap,
        "pending_actions": len(pending),
    }


# =====================================================================
# Approvals (human-in-the-loop, no model)
# =====================================================================
@app.get("/api/actions/pending")
def pending_actions(plant: str | None = None,
                    p: Principal = Depends(current_principal)) -> dict:
    from proof.tools import actions as action_tools
    return action_tools.list_pending_actions(plant)


@app.get("/api/actions/{action_id}")
def action_detail(action_id: int) -> dict:
    from proof.tools import actions as action_tools
    res = action_tools.get_action(action_id)
    if res["row_count"] == 0:
        raise HTTPException(status_code=404, detail=f"no action {action_id}")
    return res


class DecisionBody(BaseModel):
    event: str            # "approved" | "rejected"
    note: str | None = None


@app.post("/api/actions/{action_id}/decision")
def decide_action(action_id: int, body: DecisionBody,
                  p: Principal = Depends(current_principal)) -> dict:
    """Approve or reject, as the header's principal, through the shared path.

    The decision runs the exact authorization checks the CLI does -- the
    browser is not a privileged caller. A refusal comes back as 403 with the
    reason the authz layer gave.
    """
    from proof.approvals import decide

    result = decide(action_id, p.principal_id, body.event, body.note)
    if not result.ok:
        code = {"denied": 403, "not_found": 404, "expired": 409,
                "error": 400}.get(result.status, 400)
        raise HTTPException(status_code=code, detail=result.message)
    return {"status": result.status, "message": result.message,
            "action_id": result.action_id}


# =====================================================================
# Ask (runs the agent -- dormant until an LLM key is set)
# =====================================================================
class AskBody(BaseModel):
    question: str


def _model_configured() -> bool:
    # The same check as the CLI's preflight: the coordinator runs on the
    # Anthropic SDK, so only its credentials make Ask work.
    return bool(os.getenv("ANTHROPIC_API_KEY")
                or os.getenv("ANTHROPIC_AUTH_TOKEN"))


@app.get("/api/ask/status")
def ask_status() -> dict:
    """Whether the chat can actually answer yet -- the UI reads this to decide
    between an enabled composer and a 'connect a model' banner."""
    from proof.agents.config import COORDINATOR_MODEL

    return {
        "model_configured": _model_configured(),
        "coordinator_model": COORDINATOR_MODEL,
    }


@app.post("/api/ask")
async def ask_question(body: AskBody,
                       p: Principal = Depends(current_principal)):
    if not _model_configured():
        raise HTTPException(
            status_code=503,
            detail="No LLM configured. Set ANTHROPIC_API_KEY in .env to "
                   "enable Ask. The dashboard and approvals work without it.")

    from proof.agents.coordinator import ask
    from proof.agents.events import EventStreamer
    import asyncio

    streamer = EventStreamer()
    
    async def run_coordinator():
        try:
            result = await ask(body.question, principal=p, streamer=streamer)
            final_data = {
                "answer": result.answer,
                "principal": p.principal_id,
                "delegations": [{
                    "domain": r.domain,
                    "tools": [{"name": c["tool"], "explanation": c.get("explanation")} for c in r.tool_calls],
                    "citation_status": r.citation_status,
                    "authorization": getattr(r, "authorization", "allowed"),
                } for r in result.subagent_results],
                "cost_usd": round(result.budget.cost_usd, 4) if result.budget else None,
                "model_calls": result.budget.model_calls if result.budget else None,
            }
            await streamer.end(final_data)
        except Exception as e:
            import traceback
            traceback.print_exc()  # Keep full traceback in server logs
            
            # Drill down through nested ExceptionGroups to find the real error
            def get_deepest_error(exc):
                if hasattr(exc, "exceptions") and exc.exceptions:
                    return get_deepest_error(exc.exceptions[0])
                return exc
                
            deepest = get_deepest_error(e)
            error_msg = f"{type(deepest).__name__}: {str(deepest)}"
            
            await streamer.error(error_msg)
            
    asyncio.create_task(run_coordinator())
    return StreamingResponse(streamer.stream(), media_type="text/event-stream")
