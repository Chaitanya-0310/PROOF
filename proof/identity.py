"""
Who is asking.

THE RULE: identity arrives from the transport, never from the conversation.

The failure this prevents is worth stating plainly, because it is the one
every early agent demo ships with. If the assistant asks "which plant are you
at?" and trusts the answer, then the authorization model is a text field the
user controls, and anyone who can type can be anyone. The fix is not a better
prompt. It is that the model never sees, and never supplies, the identity at
all.

Here the principal is resolved once per session and injected into the MCP
server subprocesses as an environment variable at spawn time. The model
cannot read it, cannot alter it, and cannot pass a different one -- the tool
functions do not accept a principal argument, so there is nothing to forge.

In a real deployment `resolve()` would validate an OIDC token from the bank's
-- here, the manufacturer's -- identity provider. The shape of what comes out
is the same: a stable id, a role, and a plant scope.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

PRINCIPAL_ENV = "PROOF_PRINCIPAL"

# Capabilities by role, as a transcription of SOP-LINE-REALLOC section 3 and
# POL-CUSTOMER-NOTIF section 4 -- not an invention of this project. When the
# procedure changes, this table changes, and the SOP is the source of truth.
#
#   propose:*  -- may ask for an action to be queued
#   approve:*  -- may authorise a queued action
#   cross_plant -- may act outside their own plant scope
ROLE_CAPABILITIES: dict[str, set[str]] = {
    "line_operator": {
        "read",
    },
    "shift_supervisor": {
        "read",
        "propose:reallocate_run",
        "propose:notify_customer",
        "propose:expedite_purchase_order",
        "propose:hold_product",
    },
    "plant_manager": {
        "read",
        "propose:reallocate_run",
        "propose:notify_customer",
        "propose:expedite_purchase_order",
        "propose:hold_product",
        # SOP-LINE-REALLOC 3.1: within a plant, the plant manager approves.
        "approve:reallocate_run",
        "approve:hold_product",
        "approve:expedite_purchase_order",
    },
    "regional_director": {
        "read",
        "propose:reallocate_run",
        "propose:notify_customer",
        "propose:expedite_purchase_order",
        "propose:hold_product",
        "approve:reallocate_run",
        "approve:hold_product",
        "approve:expedite_purchase_order",
        # POL-CUSTOMER-NOTIF 4.1: plant staff do not contact customers. Only
        # the commercial side releases a notification, and in this model that
        # sits with the regional director.
        "approve:notify_customer",
        # SOP-LINE-REALLOC 3.2: across plants needs the regional director.
        "cross_plant",
    },
}


@dataclass(frozen=True)
class Principal:
    principal_id: str
    display_name: str
    role: str
    # Empty means every plant. Anything else is an explicit allow-list.
    plant_scope: tuple[str, ...] = field(default_factory=tuple)

    @property
    def capabilities(self) -> set[str]:
        return ROLE_CAPABILITIES.get(self.role, set())

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def in_scope(self, plant_code: str | None) -> bool:
        """Is this plant inside the principal's scope?

        A principal with `cross_plant` is in scope everywhere. Otherwise an
        empty scope means unrestricted and a populated one is an allow-list.
        `None` (no plant in question) is always in scope.
        """
        if plant_code is None or self.can("cross_plant") or not self.plant_scope:
            return True
        return plant_code in self.plant_scope

    def to_env(self) -> str:
        return json.dumps({
            "principal_id": self.principal_id,
            "display_name": self.display_name,
            "role": self.role,
            "plant_scope": list(self.plant_scope),
        })


# Demo principals. In a real deployment these come from the IdP; here they
# stand in for four people with genuinely different authority, which is what
# makes the Phase 4 demo show something rather than assert it.
DEMO_PRINCIPALS: dict[str, Principal] = {
    "a.morin": Principal("a.morin", "Alice Morin", "shift_supervisor", ("TOR1",)),
    "j.okafor": Principal("j.okafor", "Joseph Okafor", "plant_manager", ("TOR1",)),
    "s.rhodes": Principal("s.rhodes", "Sam Rhodes", "regional_director", ()),
    "t.vance": Principal("t.vance", "Tara Vance", "line_operator", ("DAL1",)),
}

DEFAULT_PRINCIPAL = "a.morin"


class NoPrincipal(RuntimeError):
    """Raised when a component that needs identity cannot find one.

    Failing closed matters here. A tool that quietly proceeds without a
    principal is a tool with no authorization at all, and it would pass every
    test written against the happy path.
    """


def resolve(principal_id: str | None = None) -> Principal:
    """Resolve the session principal. Stands in for OIDC token validation."""
    key = principal_id or os.getenv("PROOF_USER") or DEFAULT_PRINCIPAL
    if key not in DEMO_PRINCIPALS:
        raise NoPrincipal(
            f"unknown principal {key!r}; known: {sorted(DEMO_PRINCIPALS)}")
    return DEMO_PRINCIPALS[key]


def from_env() -> Principal:
    """Read the principal injected by the parent process.

    Used inside MCP server subprocesses. There is no fallback to a default:
    a server started without an identity must refuse to act, not act as
    somebody convenient.
    """
    raw = os.getenv(PRINCIPAL_ENV)
    if not raw:
        raise NoPrincipal(
            f"{PRINCIPAL_ENV} is not set. The MCP server was started without "
            "a session identity and cannot authorize anything.")
    try:
        d = json.loads(raw)
        return Principal(
            principal_id=d["principal_id"],
            display_name=d["display_name"],
            role=d["role"],
            plant_scope=tuple(d.get("plant_scope") or ()),
        )
    except (ValueError, KeyError) as exc:
        raise NoPrincipal(f"{PRINCIPAL_ENV} is malformed: {exc}") from exc
