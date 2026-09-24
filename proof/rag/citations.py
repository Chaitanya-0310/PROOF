"""
Citation enforcement.

A system prompt that says "always cite your sources" is a request. This
module is the enforcement: the quality agent's answer is parsed, every
citation in it is checked against what retrieval actually returned, and an
answer that cites something it was never shown does not reach the
coordinator intact.

That distinction matters more here than in most domains. If the agent
paraphrases a food-safety procedure slightly wrong and attributes it to
"SOP-ALLERGEN-CO section 4", a quality manager who trusts the citation has
been handed a fabricated instruction with a real-looking reference on it.
The failure is worse than no answer, because the reference is what makes it
credible.

Three outcomes:

  ok          -- every citation matches a retrieved chunk
  fabricated  -- a citation that was never retrieved; the answer is REPLACED,
                 because a wrong procedure reference is worse than silence
  uncited     -- procedural claims with no citation at all; the answer is
                 passed through but flagged UNVERIFIED so the coordinator
                 will not quote it as procedure
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# 'SOP-RESTART-FLAT §4.2', and the ASCII fallbacks a model reaches for when
# it cannot produce the section sign: 'SOP-RESTART-FLAT section 4.2',
# 'WI-SHEETER sec 3'. All three are accepted and normalised to one form --
# rejecting an answer for typographic reasons would be enforcement theatre.
CITATION = re.compile(
    r"\b(?P<doc>[A-Z]{2,4}(?:-[A-Z0-9]+)+)\s*"
    r"(?:§|section\s+|sec\.?\s+|#)\s*"
    r"(?P<sec>\d+(?:\.\d+)*)",
    re.IGNORECASE,
)

# Words that mean the agent is asserting a procedural requirement. If any of
# these appear and nothing is cited, the answer is making rules up.
PROCEDURAL = re.compile(
    r"\b(must|required|requires|shall|prohibited|not permitted|approval|"
    r"approve[sd]?|sign-?off|mandatory|minimum|before\s+(?:restart|release))\b",
    re.IGNORECASE,
)


@dataclass
class CitationCheck:
    status: str                              # ok | fabricated | uncited | n/a
    cited: list[str] = field(default_factory=list)
    fabricated: list[str] = field(default_factory=list)
    available: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "n/a")


def normalise(doc: str, sec: str) -> str:
    return f"{doc.upper()}§{sec}"


def extract_citations(text: str) -> list[str]:
    """Every citation the answer claims, normalised for comparison."""
    seen: list[str] = []
    for m in CITATION.finditer(text or ""):
        key = normalise(m.group("doc"), m.group("sec"))
        if key not in seen:
            seen.append(key)
    return seen


def citations_from_tool_results(raw_results: list[str]) -> list[str]:
    """The citations retrieval actually returned -- the ground truth set.

    Reads the `citation` column out of each tool result. Anything the answer
    cites that is not in here was not retrieved, whatever else it may be.
    """
    available: list[str] = []
    for raw in raw_results:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for row in payload.get("rows", []) or []:
            cit = row.get("citation")
            if not isinstance(cit, str):
                continue
            m = CITATION.search(cit)
            key = normalise(m.group("doc"), m.group("sec")) if m else cit.upper()
            if key not in available:
                available.append(key)
    return available


def check(answer: str, available: list[str]) -> CitationCheck:
    """Validate an answer's citations against what was retrieved."""
    # Nothing was retrieved, so nothing is claimed about procedure. This is
    # the normal case for the structured quality tools (allergen checks,
    # holds), which answer from SQL and have nothing to cite.
    if not available:
        return CitationCheck(status="n/a")

    cited = extract_citations(answer)
    fabricated = [c for c in cited if c not in available]

    if fabricated:
        return CitationCheck(status="fabricated", cited=cited,
                             fabricated=fabricated, available=available)
    if not cited and PROCEDURAL.search(answer or ""):
        return CitationCheck(status="uncited", cited=cited, available=available)
    return CitationCheck(status="ok", cited=cited, available=available)


def enforce(answer: str, result: CitationCheck) -> str:
    """Apply the consequence. This is the part that makes it enforcement.

    A fabricated citation REPLACES the answer. It is tempting to annotate and
    pass it through, but a plausible procedural instruction carrying a
    reference the reader cannot distinguish from a real one is precisely the
    output this system must never emit.
    """
    if result.status == "fabricated":
        return (
            "CITATION CHECK FAILED -- answer withheld.\n"
            f"The quality agent cited {', '.join(result.fabricated)}, which "
            "was not returned by retrieval and may not exist. The answer has "
            "been withheld rather than shown with an unverifiable reference.\n"
            f"Sections actually retrieved: {', '.join(result.available) or 'none'}.\n"
            "Re-ask with a narrower question, or read the sections above directly."
        )
    if result.status == "uncited":
        return (
            "[UNVERIFIED -- no citation given]\n" + answer +
            "\n\n(This answer states procedural requirements but cites no SOP "
            "section. Do not present it as procedure. Retrieved sections were: "
            f"{', '.join(result.available)}.)"
        )
    return answer
