"""
Runaway protection: a hard ceiling on one question's model usage.

Agentic loops fail in a specific, expensive way. A sub-agent misreads a tool
result, asks again, misreads again, and the loop spins -- each turn billed,
none converging. `max_iterations` on the tool runner caps ONE agent's turns,
but a coordinator that re-delegates in a loop can blow past any per-agent cap
while every individual agent stays within its own. The budget is the ceiling
across the whole question.

Two independent limits, because the two failure modes are different:

  * call count -- catches a tight logical loop early, before it costs much;
  * dollar spend -- catches a small number of very large calls.

Either tripping raises BudgetExceeded, which the coordinator turns into an
honest partial answer ("I stopped after N steps") rather than a silent
truncation. A bill that tripled overnight is the story this guards against;
an agent that stops and says why is the point.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import estimate_cost


class BudgetExceeded(RuntimeError):
    """Raised when a session hits its call or spend ceiling."""


@dataclass
class SessionBudget:
    # Defaults sized for the demos: a fan-out to four or five agents, each a
    # few turns, is ~15-20 model calls. 40 leaves headroom for a genuinely
    # hard question while still catching a loop. $1.00 is far above any real
    # single question here and well below a runaway.
    max_model_calls: int = 40
    max_cost_usd: float = 1.00

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    _tripped: str | None = field(default=None)

    @property
    def cost_usd(self) -> float:
        return estimate_cost(self.input_tokens, self.output_tokens)

    @property
    def tripped(self) -> str | None:
        return self._tripped

    def record(self, input_tokens: int, output_tokens: int) -> None:
        """Count one model call. Call AFTER each turn, before the next.

        Recording happens even on the call that trips the budget -- the tokens
        were really spent, so they belong in the total the user is shown.
        """
        self.model_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def check(self) -> None:
        """Raise if either ceiling is now exceeded.

        Called between turns. Checking after recording means the trip reflects
        real spend, and the raised message carries the numbers so the answer
        can say exactly where it stopped.
        """
        if self.model_calls >= self.max_model_calls:
            self._tripped = (
                f"stopped after {self.model_calls} model calls "
                f"(limit {self.max_model_calls})")
            raise BudgetExceeded(self._tripped)
        if self.cost_usd >= self.max_cost_usd:
            self._tripped = (
                f"stopped at ${self.cost_usd:.2f} of model spend "
                f"(limit ${self.max_cost_usd:.2f})")
            raise BudgetExceeded(self._tripped)

    def remaining_calls(self) -> int:
        return max(0, self.max_model_calls - self.model_calls)
