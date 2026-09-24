"""
Retrieval eval + citation-enforcement tests. No API key needed.

Two jobs:

1. **Measure retrieval.** A labelled set of questions with the section that
   should answer them, scored as recall@5 and MRR, run three times -- hybrid,
   lexical only, dense only. If hybrid does not beat both arms, the design
   argument in retrieve.py is wrong and should be changed rather than
   defended. Claiming hybrid is better without measuring it is the thing this
   file exists to prevent.

2. **Test citation enforcement.** Including the case that matters: an answer
   citing a section that was never retrieved must be withheld, not annotated.

Questions are phrased the way a supervisor would type them mid-shift, not in
the vocabulary of the documents. A retrieval eval whose queries echo the
headings measures nothing.

Run:  make smoke-rag
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proof.rag import retrieve  # noqa: E402
from proof.rag.citations import check, citations_from_tool_results, enforce  # noqa: E402

# (question, the citation that should be retrieved, optional filters)
# The gold label is the section a quality manager would point to.
GOLD: list[tuple[str, str, dict]] = [
    ("how long can proofed dough sit before it has to go in the oven",
     "SOP-PROOF-WINDOW §4", {}),
    ("dough has gone past its proof time, can we still bake it",
     "SOP-PROOF-WINDOW §5", {}),
    ("who signs off moving production to a different line",
     "SOP-LINE-REALLOC §3", {}),
    ("do I need a full wash when changing to a product with milk in it",
     "SOP-ALLERGEN-CO §4", {}),
    ("switching to a product with no new allergens, what do I do",
     "SOP-ALLERGEN-CO §3", {}),
    ("sheeter gearbox has seized, how long will this take",
     "WI-SHEETER §3", {}),
    ("what checks before restarting a naan line after a breakdown",
     "SOP-RESTART-FLAT §4", {"category": "flatbread"}),
    ("do we need to retest the metal detector after a stoppage",
     "HACCP-METAL §6", {}),
    ("when do we have to tell the customer their order is short",
     "POL-CUSTOMER-NOTIF §3", {}),
    ("flour delivery is two days late, what do we do",
     "SOP-SUPPLIER-DEV §4", {}),
    ("how do I work out which run we cannot build",
     "SOP-SUPPLIER-DEV §3", {}),
    ("what temperature does the product need to reach in the oven",
     "HACCP-BAKE §3", {}),
    ("what reason code for dough that over-proofed during a breakdown",
     "SOP-SCRAP-DISP §2", {}),
    ("is 2 percent scrap normal",
     "SOP-SCRAP-DISP §4", {}),
    ("who can release product that is on hold",
     "SOP-HOLD-RELEASE §4", {}),
    ("donut line stopped, what do I check before starting it again",
     "SOP-RESTART-SWEET §4", {"category": "sweet_goods"}),
]

TOP_K = 5


def score(mode: str) -> tuple[float, float, list[str]]:
    """Return (recall@5, MRR, misses) for one retrieval mode."""
    hits = 0
    rr_total = 0.0
    misses: list[str] = []
    for question, gold, filters in GOLD:
        res = retrieve.search_procedures(question, top_k=TOP_K, mode=mode, **filters)
        cites = [r["citation"] for r in res["rows"]]
        if gold in cites:
            hits += 1
            rr_total += 1.0 / (cites.index(gold) + 1)
        else:
            misses.append(f"{question[:44]:46s} want {gold:24s} got {cites[:2]}")
    n = len(GOLD)
    return hits / n, rr_total / n, misses


def main() -> int:
    failures: list[str] = []

    print(f"{'=' * 72}\nretrieval eval: {len(GOLD)} labelled questions, "
          f"recall@{TOP_K}\n{'=' * 72}")

    results = {}
    for mode in ("lexical", "dense", "hybrid"):
        recall, mrr, misses = score(mode)
        results[mode] = (recall, mrr, misses)
        print(f"  {mode:8s}  recall@{TOP_K} {recall:5.1%}   MRR {mrr:.3f}   "
              f"{len(misses)} miss(es)")

    h_recall, h_mrr, h_misses = results["hybrid"]
    l_recall, l_mrr, _ = results["lexical"]
    d_recall, d_mrr, _ = results["dense"]

    # --- the weight sweep --------------------------------------------
    # LEX_WEIGHT was NOT chosen by intuition. Equal-weight fusion (0.5)
    # scored worse than lexical alone, and this sweep is what found that and
    # what picked 0.7. Kept in the eval so the number can be re-derived
    # rather than taken on trust when the corpus changes.
    print(f"\n{'=' * 72}\nlex_weight sweep (0.0 = dense only, 1.0 = lexical only)"
          f"\n{'=' * 72}")
    for w in (0.0, 0.3, 0.5, 0.6, 0.7, 0.8, 1.0):
        hits = rr = 0.0
        for question, gold, filters in GOLD:
            res = retrieve.search_procedures(question, top_k=TOP_K, mode="hybrid",
                                             lex_weight=w, **filters)
            cites = [r["citation"] for r in res["rows"]]
            if gold in cites:
                hits += 1
                rr += 1.0 / (cites.index(gold) + 1)
        marker = "  <- LEX_WEIGHT" if abs(w - retrieve.LEX_WEIGHT) < 1e-9 else ""
        print(f"  w={w:.1f}  recall@{TOP_K} {hits / len(GOLD):5.1%}   "
              f"MRR {rr / len(GOLD):.3f}{marker}")

    # The claim retrieve.py makes, as an assertion.
    #
    # Recall must not be worse than the best single arm, and MRR must be
    # strictly better -- that second condition is where the dense arm earns
    # its place on this corpus. It does not find MORE of the right sections
    # than lexical does, it ranks the ones both arms find higher.
    if h_recall < max(l_recall, d_recall):
        failures.append(
            f"hybrid recall {h_recall:.1%} is WORSE than the best single arm "
            f"(lexical {l_recall:.1%}, dense {d_recall:.1%})")
    if h_mrr <= max(l_mrr, d_mrr):
        failures.append(
            f"hybrid MRR {h_mrr:.3f} does not beat the best single arm "
            f"(lexical {l_mrr:.3f}, dense {d_mrr:.3f}) -- fusion is not "
            f"earning its cost")
    if h_recall < 0.80:
        failures.append(f"hybrid recall@{TOP_K} {h_recall:.1%} below the 80% bar")

    if h_misses:
        print("\n  hybrid misses:")
        for m in h_misses:
            print(f"    {m}")

    # --- scoping ------------------------------------------------------
    print(f"\n{'=' * 72}\ndocument scoping\n{'=' * 72}")
    sweet = retrieve.search_procedures(
        "what checks before restarting the line", top_k=5, category="sweet_goods")
    sweet_cites = [r["citation"] for r in sweet["rows"]]
    print(f"  category=sweet_goods -> {sweet_cites[:3]}")
    # A flatbread-only restart procedure quoted at a sweet goods line is a
    # real food-safety error, not a ranking nuisance.
    leaked = [c for c in sweet_cites if c.startswith("SOP-RESTART-FLAT")]
    if leaked:
        failures.append(f"flatbread-only procedure leaked into sweet_goods scope: {leaked}")
    else:
        print("  no flatbread-only procedure leaked into the sweet_goods scope")

    # --- citation enforcement ----------------------------------------
    print(f"\n{'=' * 72}\ncitation enforcement\n{'=' * 72}")
    real = retrieve.search_procedures("who approves a line move", top_k=3)
    available = citations_from_tool_results([
        __import__("json").dumps({"rows": real["rows"]})
    ])
    good_cite = real["rows"][0]["citation"]
    print(f"  retrieved: {', '.join(available)}")

    cases = [
        ("honest answer cites a retrieved section",
         f"Approval sits with the plant manager per {good_cite}.", "ok", False),
        ("fabricated citation is withheld",
         "You must get sign-off from the director per SOP-GHOST §9.2.",
         "fabricated", True),
        ("procedural claim with no citation is flagged unverified",
         "The plant manager must approve this before it proceeds.",
         "uncited", False),
        ("non-procedural text needs no citation",
         "Line 5 is currently idle.", "ok", False),
    ]
    for label, answer, expect_status, expect_replaced in cases:
        res = check(answer, available)
        out = enforce(answer, res)
        replaced = answer not in out
        ok = res.status == expect_status and replaced == expect_replaced
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:52s} -> {res.status}")
        if not ok:
            failures.append(
                f"{label}: status {res.status} (want {expect_status}), "
                f"replaced={replaced} (want {expect_replaced})")

    # 'section 9.2' and '#9.2' spellings must be caught too -- a model that
    # cannot type the section sign should not thereby evade the check.
    for spelling in ["SOP-GHOST section 9.2", "SOP-GHOST #9.2", "sop-ghost §9.2"]:
        res = check(f"You must do this per {spelling}.", available)
        ok = res.status == "fabricated"
        print(f"  [{'PASS' if ok else 'FAIL'}] fabricated via '{spelling}'")
        if not ok:
            failures.append(f"citation spelling '{spelling}' evaded the check")

    print(f"\n{'=' * 72}")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all checks passed  (hybrid recall@{TOP_K} {h_recall:.1%}, "
          f"MRR {h_mrr:.3f})")
    print(f"\nHonest limits of this eval: {len(GOLD)} questions, single gold "
          f"label each.\nSeveral questions have a defensible second answer "
          f"that is scored as a miss,\nand a weight tuned on 16 questions is "
          f"tuned on 16 questions. It is enough to\ncatch a broken arm or a "
          f"regression; it is not enough to claim a general result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
