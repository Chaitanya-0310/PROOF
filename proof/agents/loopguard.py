"""Stop a tool loop that is going nowhere, and say why.

Two failure shapes were seen in practice, both ending in "(no answer
produced)" after every iteration was spent:

1. **Empty arguments.** The model gateway intermittently returned tool calls
   with `{}` as input. The call fails validation, the model re-issues it,
   the gateway empties it again -- ten coordinator rounds with no agent ever
   asked, or eight sub-agent rounds of the same two failing queries.
2. **Identical repeats.** The same calls with the same arguments, turn after
   turn. The tool will return what it returned last time.

max_iterations does end both, but silently and only after the whole budget
of rounds is gone. This ends them after `limit` consecutive bad turns and
hands back a reason a person can act on.
"""
from __future__ import annotations

import json
from typing import Any


class LoopGuard:
    def __init__(self, required: dict[str, list[str]], limit: int = 2):
        # tool name -> the argument names its schema requires
        self.required = required
        self.limit = limit
        self._empty_streak = 0
        self._repeat_streak = 0
        self._last_sig: tuple | None = None

    def check(self, message: Any) -> str | None:
        """Feed each model turn in order. Returns a reason to stop, or None."""
        calls = [b for b in message.content if b.type == "tool_use"]
        if not calls:
            self._empty_streak = self._repeat_streak = 0
            self._last_sig = None
            return None

        missing = [b.name for b in calls
                   if any(f not in (b.input or {})
                          for f in self.required.get(b.name, ()))]
        self._empty_streak = self._empty_streak + 1 if len(missing) == len(calls) else 0

        sig = tuple(sorted((b.name, json.dumps(b.input or {}, sort_keys=True))
                           for b in calls))
        self._repeat_streak = self._repeat_streak + 1 if sig == self._last_sig else 0
        self._last_sig = sig

        if self._empty_streak >= self.limit:
            return (f"the model sent {', '.join(sorted(set(missing)))} with "
                    f"missing arguments {self._empty_streak} turns in a row")
        if self._repeat_streak >= self.limit:
            return (f"the model repeated the same tool calls "
                    f"({', '.join(sorted({b.name for b in calls}))}) "
                    f"{self._repeat_streak + 1} turns in a row")
        return None


def required_args(schemas: dict[str, dict]) -> dict[str, list[str]]:
    """tool name -> required argument names, from JSON input schemas."""
    return {name: list((schema or {}).get("required") or [])
            for name, schema in schemas.items()}
