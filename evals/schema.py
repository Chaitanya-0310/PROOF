"""
The record a grader judges, and the shape a grade comes back in.

The important design choice: graders operate on a `RunRecord`, not on a live
agent. A RunRecord is a flat, serialisable snapshot of what one question
produced -- the final answer, which domains were consulted and what tools they
called, each domain's citation outcome, and the budget. It can come from a
live coordinator run OR be hand-written as a fixture.

That split is what lets the grading logic be tested without a model. A grader
that only works against a live API is a grader nobody re-runs; one that runs
against a fixture is checked on every commit. So the same grader that scores a
real run also proves, offline, that it can tell a good answer from a broken
one.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class DomainRecord:
    """What one sub-agent did, distilled to what a grader needs."""
    domain: str
    tools_called: list[str] = field(default_factory=list)
    citation_status: str = "n/a"           # ok | fabricated | uncited | n/a
    authorization: str = "allowed"         # allowed | denied


@dataclass
class RunRecord:
    """One question, its answer, and how it was produced."""
    case_id: str
    question: str
    principal: str
    answer: str
    domains: list[DomainRecord] = field(default_factory=list)
    model_calls: int = 0
    cost_usd: float = 0.0
    budget_tripped: str | None = None

    # --- convenience accessors the graders lean on ------------------
    def domain(self, name: str) -> DomainRecord | None:
        return next((d for d in self.domains if d.domain == name), None)

    def called(self, domain: str, tool: str) -> bool:
        d = self.domain(domain)
        return bool(d and tool in d.tools_called)

    def consulted(self, domain: str) -> bool:
        return self.domain(domain) is not None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        domains = [DomainRecord(**x) for x in d.get("domains", [])]
        return cls(**{**d, "domains": domains})


@dataclass
class GradeResult:
    grader: str
    passed: bool
    reason: str
    # 'must' failures fail the case; 'should' failures are reported but do not.
    # The distinction keeps a stylistic miss from masking a correctness break.
    severity: str = "must"
