"""
Hybrid retrieval over the SOP corpus: lexical + dense, fused with RRF.

Why hybrid, rather than picking one:

**Dense alone misses the vocabulary that matters here.** "sheeter", "82 C",
"CHANGEOVER_PURGE", "swab" -- plant and food-safety terms are exactly where
an embedding trained on general web text is weakest, and they are exactly the
terms an operator types. Lexical search nails them.

**Lexical alone misses the question as asked.** A supervisor types "can I
move this to another line", and the governing section is titled "Approval
authority". No term overlaps. Dense retrieval finds it.

Fusion is **weighted Reciprocal Rank Fusion**: score each result by
w/(k + rank) in each list and add. RRF is used rather than blending the raw
scores because the two scorers produce incomparable numbers -- ts_rank_cd is
unbounded and corpus-dependent, cosine distance is [0,2] -- so no fixed
threshold between them survives a corpus change. RRF needs only the ORDER of
each list.

**What the eval changed.** This module originally fused the two arms equally
and asserted that hybrid beats either alone. The eval in
`scripts/smoke_rag.py` disproved it: equal-weight fusion scored 81.2%
recall@5 against lexical-alone's 87.5%, because the dense arm is weaker on
this corpus and its confident-but-wrong hits displaced correct lexical ones.
Sweeping the weight gives the real picture:

    lexical only   recall 87.5%   MRR 0.604
    dense only     recall 75.0%   MRR 0.460
    hybrid w=0.5   recall 81.2%   MRR 0.565     <- worse than lexical alone
    hybrid w=0.7   recall 87.5%   MRR 0.658     <- current setting

So the honest claim is narrower than the one this file used to make: on this
corpus the dense arm does not find MORE of the right sections than lexical
does. What it does is rank the sections both arms find HIGHER, which is why
w=0.7 matches lexical's recall while clearly beating its MRR. Fusion earns
its place on ranking quality, not coverage.

A second note on honesty: the lexical half is Postgres `ts_rank_cd`, which is
not textbook BM25 -- it has no document-length saturation term. It is a real
lexical scorer and it keeps the entire hybrid query inside one engine, which
on a corpus this size is worth more than the difference.
"""
from __future__ import annotations

import functools
from typing import Any

from proof.tools._base import query

# RRF smoothing constant. 60 is the value from the original Cormack et al.
# paper and is not sensitive on a corpus this small; it mainly controls how
# steeply rank 1 outweighs rank 10.
RRF_K = 60

# Weight on the lexical arm; the dense arm gets (1 - LEX_WEIGHT).
#
# This started at an implicit 0.5 and the eval rejected it: equal-weight
# fusion scored WORSE than lexical alone (81.2% vs 87.5% recall@5), because
# the dense arm is materially weaker on this corpus and its confident-but-
# wrong hits displaced correct lexical ones. See scripts/smoke_rag.py, which
# sweeps this value and asserts hybrid beats both single arms.
#
# The module docstring argues RRF needs no tuned constant. That is true of
# score NORMALISATION -- RRF needs only ranks -- but arm weighting is still a
# tunable, and pretending otherwise was wrong. The defensible position is not
# "no constants", it is "no constants chosen without measurement".
LEX_WEIGHT = 0.7


@functools.lru_cache(maxsize=1)
def _model():
    """Load the embedding model once per process.

    lru_cache rather than a module global so the import cost is only paid if
    something actually embeds -- the MCP server process that never receives a
    search call never loads torch.
    """
    from sentence_transformers import SentenceTransformer

    from .index import EMBED_MODEL
    return SentenceTransformer(EMBED_MODEL, device="cpu")


def embed_query(text: str) -> str:
    """Embed a query and render it as a pgvector literal."""
    vec = _model().encode([text], normalize_embeddings=True)[0]
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def search_procedures(question: str, top_k: int = 5,
                      category: str | None = None,
                      line_code: str | None = None,
                      doc_type: str | None = None,
                      mode: str = "hybrid",
                      lex_weight: float | None = None) -> dict[str, Any]:
    """Retrieve the SOP sections most likely to answer `question`.

    Filters are applied INSIDE both retrieval arms, not after fusion. Filtering
    afterwards would let a flatbread-only procedure occupy a slot in the top-k
    and then be dropped, returning fewer results than asked for and silently
    degrading recall on exactly the queries that were scoped most carefully.

    `mode` exists so the Phase 3 eval can run each arm alone and MEASURE
    whether hybrid actually beats either one. A design argument that cannot
    be measured is just a preference. It is deliberately not exposed as an
    MCP tool parameter -- the agent should never be choosing a retrieval
    strategy.
    """
    if mode not in ("hybrid", "lexical", "dense"):
        raise ValueError(f"mode must be hybrid, lexical or dense; got {mode!r}")
    w = LEX_WEIGHT if lex_weight is None else lex_weight
    qvec = embed_query(question)

    return query("""
        WITH filtered AS (
            SELECT c.chunk_id, c.doc_id, c.section_no, c.section_title,
                   c.content, c.citation, c.tsv, c.embedding
            FROM docs.chunks c
            JOIN docs.documents d ON d.doc_id = c.doc_id
            WHERE (%(category)s::text IS NULL
                   OR d.applies_to_categories = '{}'
                   OR %(category)s = ANY (d.applies_to_categories))
              AND (%(line_code)s::text IS NULL
                   OR d.applies_to_lines = '{}'
                   OR %(line_code)s = ANY (d.applies_to_lines))
              AND (%(doc_type)s::text IS NULL OR d.doc_type = %(doc_type)s)
        ),
        -- The tsquery is built with OR semantics, and that detail is the
        -- difference between a working lexical arm and a dead one.
        --
        -- websearch_to_tsquery joins terms with AND, so a nine-word question
        -- demands all nine stems in a single chunk and matches nothing. The
        -- first version of this file shipped that way: every result came
        -- back dense-only, the RRF scores collapsed to the 1/(k+rank) series
        -- of a single list, and the "hybrid" retrieval was hybrid in name.
        --
        -- Swapping '&' for '|' in the rendered tsquery keeps the parser's
        -- stemming, stopword removal and phrase (<->) handling, and only
        -- changes the combinator -- which is exactly the one thing that was
        -- wrong.
        q AS (
            SELECT replace(
                       websearch_to_tsquery('english', %(q)s)::text, '&', '|'
                   )::tsquery AS tsq
        ),
        lexical AS (
            SELECT c.chunk_id,
                   row_number() OVER (
                       ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC, c.chunk_id
                   ) AS rank
            FROM filtered c, q
            WHERE c.tsv @@ q.tsq
            LIMIT 20
        ),
        dense AS (
            SELECT chunk_id,
                   row_number() OVER (
                       ORDER BY embedding <=> %(qvec)s::vector
                   ) AS rank
            FROM filtered
            ORDER BY embedding <=> %(qvec)s::vector
            LIMIT 20
        ),
        fused AS (
            SELECT COALESCE(l.chunk_id, dn.chunk_id) AS chunk_id,
                   COALESCE(CASE WHEN %(use_lex)s
                                 THEN %(w_lex)s / (%(k)s + l.rank) END, 0)
                   + COALESCE(CASE WHEN %(use_den)s
                                   THEN %(w_den)s / (%(k)s + dn.rank) END, 0)
                       AS rrf_score,
                   l.rank AS lexical_rank,
                   dn.rank AS dense_rank
            FROM lexical l
            FULL OUTER JOIN dense dn ON dn.chunk_id = l.chunk_id
        )
        SELECT f.chunk_id, c.citation, d.title AS document_title,
               d.doc_type, d.version, d.effective_date,
               c.section_no, c.section_title, c.content,
               round(f.rrf_score::numeric, 5) AS rrf_score,
               f.lexical_rank, f.dense_rank
        FROM fused f
        JOIN docs.chunks c    ON c.chunk_id = f.chunk_id
        JOIN docs.documents d ON d.doc_id = c.doc_id
        WHERE f.rrf_score > 0
        ORDER BY f.rrf_score DESC, c.citation
        LIMIT %(top_k)s
    """, {"q": question, "qvec": qvec, "k": RRF_K, "top_k": top_k,
          "category": category, "line_code": line_code, "doc_type": doc_type,
          "use_lex": mode in ("hybrid", "lexical"),
          "use_den": mode in ("hybrid", "dense"),
          # Single-arm modes use full weight so their scores stay
          # comparable across modes in the eval.
          "w_lex": 1.0 if mode == "lexical" else w,
          "w_den": 1.0 if mode == "dense" else 1.0 - w},
        note="Quote ONLY from `content`, and cite using the exact `citation` "
             "string on the same row. A result found by only one arm "
             "(lexical_rank or dense_rank is null) is still valid -- that is "
             "hybrid retrieval working, not a weak match.")


def get_document_section(citation: str) -> dict[str, Any]:
    """Fetch one section verbatim by its citation string.

    The follow-up to a search hit: read the whole section rather than the
    retrieved excerpt before relying on it.
    """
    return query("""
        SELECT c.citation, d.doc_id, d.title AS document_title, d.doc_type,
               d.version, d.effective_date, d.owner,
               c.section_no, c.section_title, c.content
        FROM docs.chunks c
        JOIN docs.documents d ON d.doc_id = c.doc_id
        WHERE c.citation = %s
    """, (citation,))


def list_documents(doc_type: str | None = None) -> dict[str, Any]:
    """What procedures exist, and what they apply to."""
    return query("""
        SELECT d.doc_id, d.title, d.doc_type, d.version, d.effective_date,
               d.owner, d.applies_to_categories, d.applies_to_lines,
               count(c.chunk_id) AS sections
        FROM docs.documents d
        LEFT JOIN docs.chunks c ON c.doc_id = d.doc_id
        WHERE (%s::text IS NULL OR d.doc_type = %s)
        GROUP BY d.doc_id, d.title, d.doc_type, d.version, d.effective_date,
                 d.owner, d.applies_to_categories, d.applies_to_lines
        ORDER BY d.doc_id
    """, (doc_type, doc_type),
        note="An empty applies_to_categories or applies_to_lines means the "
             "document applies everywhere, not nowhere.")
