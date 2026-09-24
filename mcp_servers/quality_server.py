"""Quality MCP server -- holds and allergen rules.

Phase 2 covers the STRUCTURED half of quality. The procedural half -- what an
SOP actually requires -- is RAG with mandatory citations, and arrives in
Phase 3 as additional tools on this same server.
"""
from __future__ import annotations

from _serve import build, text_result

from proof.rag import retrieve
from proof.tools import quality

server = build(
    "proof-quality",
    "Quality holds, allergen rules, and the SOP / HACCP / policy corpus. "
    "Allergen compatibility is a HARD constraint: moving a SKU onto a line "
    "not already running its allergens requires a full wet wash, materially "
    "longer than the scheduled dry changeover. Never recommend a line move "
    "without checking it. "
    "For anything procedural -- what a restart requires, who approves a "
    "reallocation, what a HACCP limit is -- use search_procedures and QUOTE "
    "the retrieved text. Every procedural statement must carry the exact "
    "`citation` string from the row it came from. Citations are checked "
    "against what retrieval returned; an answer citing a section that was "
    "not retrieved is withheld entirely. Never answer procedure from memory.",
)


@server.tool()
def get_open_quality_holds(plant_code: str) -> str:
    """Open QA holds at a plant and what they are blocking.

    Args:
        plant_code: Plant code, e.g. TOR1.
    """
    return text_result(quality.get_open_quality_holds(plant_code))


@server.tool()
def check_allergen_compatibility(from_line_id: int, to_sku_id: int) -> str:
    """Can this SKU follow what the line is currently running?

    Returns added_allergens -- the allergens the target SKU introduces that
    the line was not already running. If that list is non-empty a full wet
    allergen wash is required and the real changeover exceeds the scheduled
    one. Direction matters: milk+egg to wheat-only is cheap, the reverse
    is not.

    Args:
        from_line_id: Line whose current SKU is being switched away from.
        to_sku_id: SKU being proposed.
    """
    return text_result(
        quality.check_allergen_compatibility(from_line_id, to_sku_id))


@server.tool()
def get_sku_allergens(sku_code: str | None = None,
                      category: str | None = None) -> str:
    """Allergen profile, shelf life and proof window for SKUs.

    Args:
        sku_code: One SKU code. Omit to list by category.
        category: flatbread, artisan or sweet_goods. Omit for all.
    """
    return text_result(quality.get_sku_allergens(sku_code, category))


# --- Phase 3: procedural retrieval -------------------------------------


@server.tool()
def search_procedures(question: str, top_k: int = 5,
                      category: str | None = None,
                      line_code: str | None = None,
                      doc_type: str | None = None) -> str:
    """Search SOPs, work instructions, HACCP plans and policies.

    Hybrid retrieval: lexical + semantic, fused. Returns whole numbered
    sections, each with an exact `citation` string.

    QUOTE ONLY from the returned `content`, and cite using the `citation`
    value from that same row, verbatim. Citations are validated against these
    results -- citing anything else causes the answer to be withheld.

    Args:
        question: What you need to know, in plain English.
        top_k: How many sections to return (default 5).
        category: Restrict to flatbread, artisan or sweet_goods. Procedures
            differ by category and quoting the wrong one is a real error.
        line_code: Restrict to a line, e.g. 'TOR1/L3'.
        doc_type: Restrict to sop, work_instruction, haccp or policy.
    """
    return text_result(retrieve.search_procedures(
        question, top_k, category, line_code, doc_type))


@server.tool()
def get_document_section(citation: str) -> str:
    """Read one section verbatim by its citation string.

    Use after search_procedures when an excerpt looks decisive and you want
    the full section before relying on it.

    Args:
        citation: Exact citation string, e.g. 'SOP-RESTART-FLAT §4'.
    """
    return text_result(retrieve.get_document_section(citation))


@server.tool()
def list_procedures(doc_type: str | None = None) -> str:
    """List every procedure document, what it covers and what it applies to.

    Use to orient before searching, or to confirm whether a procedure exists
    at all.

    Args:
        doc_type: Restrict to sop, work_instruction, haccp or policy.
    """
    return text_result(retrieve.list_documents(doc_type))


if __name__ == "__main__":
    server.run("stdio")
