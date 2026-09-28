"""
Semantic-cache tests. No API key, and no running Redis needed.

The cache is an optimisation, but it sits directly on the identity boundary
this project is built to defend, so it gets the same treatment as the tools: a
runnable test that pins the properties, not a paragraph claiming them.

The two that matter, and that the naive `{domain}:{question}` cache got wrong:

  1. An entry produced for one principal is NEVER served to another. A cache
     that leaks one operator's plant-scoped answer to another quietly undoes
     the whole Phase 4 authorization model.

  2. Two questions that differ only in a FILTER -- a different line, category
     or threshold -- never collide, however similar the wording. This is the
     "white shirts under 100" vs "black shirts under 100" problem: the filter
     is extracted into the partition key, and semantic matching only runs
     inside a partition, so it can smooth phrasing but never cross a filter.

Redis is stubbed with an in-memory hash so this runs in CI unchanged. The
embedding model (all-MiniLM, offline, shared with RAG) loads if present; if it
cannot, the cache falls back to exact-text matching and the assertions that
depend on paraphrase similarity are skipped rather than failed.

Run:  make smoke-cache
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import proof.agents.cache as cache  # noqa: E402
from proof.agents.subagent import SubAgentResult  # noqa: E402
from proof.identity import resolve  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check_that(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def head(t: str) -> None:
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


class _FakeRedis:
    """The handful of hash ops the cache uses, in memory. No network."""

    def __init__(self) -> None:
        self.h: dict[str, dict[str, str]] = {}

    def hgetall(self, k):
        return dict(self.h.get(k, {}))

    def hset(self, k, f, v):
        self.h.setdefault(k, {})[f] = v

    def expire(self, k, ttl):
        pass

    def hdel(self, k, *fs):
        for f in fs:
            self.h.get(k, {}).pop(f, None)


def _result(answer: str, **kw) -> SubAgentResult:
    return SubAgentResult(domain=kw.pop("domain", "production"), answer=answer,
                          tool_calls=[], **kw)


def main() -> int:
    a = resolve("a.morin")
    j = resolve("j.okafor")
    have_embeddings = cache._embed("probe") is not None

    # ---- state extraction: the intent/filter classifier -----------------
    head("state extraction — filters that change the answer become the key")
    s_line3 = cache.extract_state("show me scrap on line 3 under 2%")
    s_line3b = cache.extract_state("what's the scrap for line 3 below 2 percent")
    s_line5 = cache.extract_state("show me scrap on line 5 under 2%")
    check_that(s_line3 == s_line3b,
               "a paraphrase with the same filters yields the same state",
               f"{s_line3}")
    check_that(s_line3 != s_line5,
               "changing the line changes the state (no collision)",
               f"line3={s_line3} line5={s_line5}")
    check_that(cache.extract_state("sweet goods scrap")
               != cache.extract_state("flatbread scrap"),
               "changing the product category changes the state")
    check_that("run:923" in cache.extract_state("what is at risk for run 923"),
               "identifiers (run 923) are captured as hard state")

    # ---- fail-open: no Redis -> no crash --------------------------------
    head("fail-open — a cache outage must never break the answer path")
    cache._redis_singleton = None
    check_that(cache.get_cached_subagent_response(a, "production", "x") is None,
               "get with no backend returns None instead of raising")
    cache.set_cached_subagent_response(a, "production", "x", _result("y"))
    check_that(True, "set with no backend is a silent no-op")

    # ---- live behaviour against the in-memory stub ----------------------
    head("lookup — identity and filters both gate a hit")
    cache._redis_singleton = _FakeRedis()
    scrap = _result("SCRAP=2.1%")
    cache.set_cached_subagent_response(a, "production",
                                       "what is the scrap on line 3", scrap)

    exact = cache.get_cached_subagent_response(a, "production",
                                               "what is the scrap on line 3")
    check_that(exact is not None and "SCRAP=2.1%" in exact.answer,
               "identical question, same principal -> hit")

    other_principal = cache.get_cached_subagent_response(
        j, "production", "what is the scrap on line 3")
    check_that(other_principal is None,
               "SAME question, DIFFERENT principal -> miss (no cross-identity leak)")

    other_line = cache.get_cached_subagent_response(
        a, "production", "what is the scrap on line 5")
    check_that(other_line is None,
               "different line -> different partition -> miss")

    if have_embeddings:
        para = cache.get_cached_subagent_response(
            a, "production", "tell me line 3 scrap")
        check_that(para is not None and "SCRAP=2.1%" in para.answer,
                   "paraphrase, same filters -> semantic hit")

        # A different intent sharing the SAME partition (identical filters)
        # must fetch its own entry, never the other one. The invariant is
        # nearest-wins: a hit is always the right intent, and a weak paraphrase
        # is allowed to miss (safe) but never to cross intents (unsafe).
        risk = _result("ATRISK=SO-100119")
        cache.set_cached_subagent_response(a, "production",
                                           "what is at risk on line 3", risk)
        got_risk = cache.get_cached_subagent_response(
            a, "production", "which orders are at risk on line 3")
        got_scrap = cache.get_cached_subagent_response(
            a, "production", "what's the scrap on line 3")
        check_that(got_risk is not None and "ATRISK" in got_risk.answer,
                   "at-risk paraphrase fetches the at-risk entry, not the scrap one")
        check_that(got_scrap is not None and "SCRAP" in got_scrap.answer,
                   "scrap paraphrase fetches the scrap entry, not the at-risk one")
    else:
        print("  [skip] embedding model unavailable — paraphrase assertions skipped")

    # ---- what must never be cached --------------------------------------
    head("exclusions — denials and empty answers are not reusable")
    cache._redis_singleton = _FakeRedis()
    cache.set_cached_subagent_response(
        a, "actions", "reallocate run 923",
        _result("denied", domain="actions", authorization="denied"))
    check_that(cache.get_cached_subagent_response(
        a, "actions", "reallocate run 923") is None,
        "an authorization DENIAL is never cached (re-decided each time)")

    cache.set_cached_subagent_response(
        a, "production", "empty case", _result("(no answer produced)"))
    check_that(cache.get_cached_subagent_response(
        a, "production", "empty case") is None,
        "an empty / no-answer result is never cached")

    cache.set_cached_subagent_response(
        a, "quality", "cite something",
        _result("per SOP-GHOST", domain="quality", citation_status="fabricated"))
    check_that(cache.get_cached_subagent_response(
        a, "quality", "cite something") is None,
        "a fabricated-citation answer is never cached")

    # ---- summary --------------------------------------------------------
    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
