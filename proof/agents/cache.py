"""Intent-partitioned semantic cache for sub-agent delegations.

The naive version of this cache keyed on `{domain}:{question}` alone. That was
wrong in two ways that matter to this project specifically:

1. **It ignored identity.** The whole Phase 4 argument is that authority is a
   property of *who is asking*, resolved from the transport and never from the
   chat. A cache that hands one principal another principal's sub-agent result
   -- with that result's plant-scoped rows and its authorization outcome --
   quietly undoes exactly that control. The principal is now part of the key,
   so a cache entry can only ever be served back to the same principal that
   produced it.

2. **It could not tell two questions apart when only a filter changed.** Plain
   semantic (or exact-string) matching treats "scrap on line 3" and "scrap on
   line 5" as near-identical text, so a fuzzy match returns the wrong line's
   number. The classic form of the bug is retail -- "white shirts under 100"
   vs "black shirts under 100" -- and it is the same shape here.

The fix is a two-stage lookup:

    STATE (exact)         SEMANTIC (fuzzy)
    principal             paraphrase of the same question
    domain                "restart the flatbread line" ==
    plant / line / run       "how do I bring the flatbread line back up"
    PO / SO / SKU ids
    numeric thresholds
    product category

First we extract the **hard state** from the question -- the identifiers,
numbers and category words that, if they differ, make it a *different*
question no matter how similar the wording. That state (plus the principal and
domain) selects a partition. Only *within* a partition do we run a semantic
similarity search over previously-cached questions, which absorbs pure
rephrasing. So "line 3" and "line 5" land in different partitions and can never
collide, while two phrasings of the line-3 question share one.

Everything here fails **open**: any Redis or embedding error skips the cache
and lets the real sub-agent run. A cache is an optimisation; it must never be
the reason an operations question cannot be answered.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict

from proof.agents.subagent import SubAgentResult
from proof.identity import Principal

# --- Tunables (env-overridable) -------------------------------------------
# A cache of plant state must expire. The plant moves, actions get approved,
# the sim clock advances; a permanently-cached number is a stale number served
# with full confidence. 15 minutes by default.
TTL_SECONDS = int(os.getenv("PROOF_CACHE_TTL_SECONDS") or 900)
# Cosine similarity required for a hit, WITHIN an already-matched state
# partition. Calibrated against all-MiniLM-L6-v2 on this domain: genuine
# paraphrases of one question ("scrap on line 3" / "the scrap for line 3")
# score ~0.75-0.85, while two DIFFERENT intents that happen to share a
# partition ("scrap on line 3" vs "what's at risk on line 3") score ~0.35-0.47.
# 0.65 sits above that second cluster with margin, so the cache leans to
# precision: a borderline paraphrase re-runs the sub-agent (safe, slightly
# slower) rather than risking a wrong-intent answer -- which in this project is
# the cardinal sin. Set >1.0 to require exact text; raise it to be stricter.
SIM_THRESHOLD = float(os.getenv("PROOF_CACHE_SIM_THRESHOLD") or 0.65)
# Cap entries per partition so a busy partition cannot grow without bound
# between TTL sweeps.
MAX_ENTRIES_PER_PARTITION = int(os.getenv("PROOF_CACHE_MAX_ENTRIES") or 32)
# One switch to turn the whole thing off (e.g. for a determinism-sensitive
# eval run) without touching the call sites.
ENABLED = (os.getenv("PROOF_CACHE_ENABLED") or "1").lower() not in ("0", "false", "no")

# Domains that are never cached. The actions agent is the only write path: a
# cache hit there replays "queued as action 1" without queuing anything, so
# the operator is told an approval is waiting when none exists. Its reads
# (pending list, whoami) are live queue state and must not be stale either.
UNCACHED_DOMAINS = frozenset({"actions"})

_KEY_PREFIX = "agent_cache:v2"


# --- Redis, lazily and defensively ----------------------------------------
def _client():
    """Return a Redis client, or None if the module or server is unavailable.

    Never raises. A cache that takes down the answer path is worse than no
    cache, so every failure here degrades to "no caching".
    """
    global _redis_singleton
    try:
        return _redis_singleton
    except NameError:
        pass
    try:
        import redis  # noqa: PLC0415

        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT") or 6379),
            db=int(os.getenv("REDIS_DB") or 0),
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
    except Exception:  # noqa: BLE001 -- import or construction failed
        client = None
    _redis_singleton = client
    return client


# --- Stage 1: hard-state extraction (the "intent classifier") -------------
# Each pattern captures a filter that, if changed, changes the answer -- the
# analog of colour and price in "white shirts under 100". Anything NOT captured
# here (verbs, phrasing, connective words) is left to the semantic stage.
_PLANT = re.compile(r"\b([A-Z]{3}\d)\b")                 # TOR1, DAL1, MTL1
_LINE_CODE = re.compile(r"\b([Ll]\d+)\b")                # L3, l5
_LINE_WORD = re.compile(r"\bline\s+#?(\d+)\b", re.I)     # "line 3"
_RUN = re.compile(r"\brun[_\s#-]*(\d+)\b", re.I)         # run 923, run_923
_PO = re.compile(r"\b(PO-?\d+)\b", re.I)                 # PO-200001
_SO = re.compile(r"\b(SO-?\d+)\b", re.I)                 # SO-100119
_SKU = re.compile(r"\bSKU[-_\s]?(\d+)\b", re.I)          # SKU-12, sku 12
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")               # thresholds/quantities

# Product categories carry different proof windows, so which category a
# question is about is a hard filter, not a wording choice. Aliases fold to one
# canonical token. This is the direct analog of the shirt colour.
_CATEGORY_ALIASES = {
    "sweet": "sweet_goods", "sweet goods": "sweet_goods",
    "sweet-goods": "sweet_goods", "sweetgoods": "sweet_goods",
    "flatbread": "flatbread", "flat bread": "flatbread",
    "artisan": "artisan",
}


def extract_state(question: str) -> tuple[str, ...]:
    """The hard filters in a question, as a canonical sorted tuple.

    Two questions with the same state tuple are 'the same question' for cache
    purposes and may be matched semantically; two with different tuples never
    are. Order-independent and case-normalised so wording does not leak in.
    """
    tokens: set[str] = set()
    for m in _PLANT.finditer(question):
        tokens.add(f"plant:{m.group(1).upper()}")
    for m in _LINE_CODE.finditer(question):
        tokens.add(f"line:{m.group(1).upper()}")
    for m in _LINE_WORD.finditer(question):
        tokens.add(f"line#:{m.group(1)}")
    for m in _RUN.finditer(question):
        tokens.add(f"run:{m.group(1)}")
    for m in _PO.finditer(question):
        tokens.add(f"po:{m.group(1).upper().replace('PO', 'PO-').replace('PO--', 'PO-')}")
    for m in _SO.finditer(question):
        tokens.add(f"so:{m.group(1).upper().replace('SO', 'SO-').replace('SO--', 'SO-')}")
    for m in _SKU.finditer(question):
        tokens.add(f"sku:{m.group(1)}")
    for m in _NUMBER.finditer(question):
        tokens.add(f"n:{m.group(0)}")
    low = question.lower()
    for alias, canonical in _CATEGORY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            tokens.add(f"cat:{canonical}")
    return tuple(sorted(tokens))


def _partition_key(principal: Principal, domain: str,
                   state: tuple[str, ...]) -> str:
    """The Redis key selecting the partition for this (who, domain, filters).

    principal_id is a hard component: an entry produced for one principal is
    never even a candidate for another. The state tuple is joined verbatim --
    it is already small and canonical, and a readable key is worth more here
    than a hash.
    """
    state_part = "|".join(state) if state else "-"
    return f"{_KEY_PREFIX}:{principal.principal_id}:{domain}:{state_part}"


# --- Stage 2: embeddings + cosine, both optional --------------------------
def _embed(text: str) -> list[float] | None:
    """Normalised embedding of `text`, reusing the RAG model, or None.

    Reuses proof.rag.retrieve's offline all-MiniLM model so the cache inherits
    the same no-egress property as procedure retrieval. If the model cannot
    load, we return None and the caller falls back to exact-text matching --
    still correct, because the state partition has already done the hard part.
    """
    try:
        from proof.rag.retrieve import _model  # noqa: PLC0415

        vec = _model().encode([text], normalize_embeddings=True)[0]
        return [float(v) for v in vec]
    except Exception:  # noqa: BLE001
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    """Dot product; inputs are already L2-normalised, so this IS cosine."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _cacheable(result: SubAgentResult) -> bool:
    """Only cache clean, reusable answers.

    Denials and fabricated-citation answers are deliberately excluded: a denial
    is a point-in-time authorization outcome that should be re-decided, and a
    withheld/fabricated answer is not something to serve twice.
    """
    if not result.answer or result.answer.strip() in ("", "(no answer produced)"):
        return False
    if result.authorization == "denied":
        return False
    if result.citation_status == "fabricated":
        return False
    # The loop guard's stop notice (proof.agents.loopguard) is a transient
    # model fault; caching it would replay the failure for the whole TTL.
    if result.answer.lstrip().startswith("[stopped:"):
        return False
    return True


# --- Public API -----------------------------------------------------------
def get_cached_subagent_response(principal: Principal, domain: str,
                                 question: str) -> SubAgentResult | None:
    """Return a cached sub-agent result for this exact state, if one is close
    enough in wording. None on any miss, disabled cache, or backend error."""
    if not ENABLED or domain in UNCACHED_DOMAINS:
        return None
    client = _client()
    if client is None:
        return None

    state = extract_state(question)
    pkey = _partition_key(principal, domain, state)
    try:
        entries = client.hgetall(pkey)
    except Exception:  # noqa: BLE001 -- Redis down / unreachable: fail open
        return None
    if not entries:
        return None

    qvec = _embed(question)
    qnorm = question.strip().lower()

    best_result: SubAgentResult | None = None
    best_score = -1.0
    for raw in entries.values():
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if qvec is not None and record.get("embedding"):
            score = _cosine(qvec, record["embedding"])
        else:
            # No embeddings available on either side: exact normalised text.
            score = 1.0 if record.get("question", "").strip().lower() == qnorm else 0.0
        if score > best_score:
            best_score = score
            best_result = _rehydrate(record.get("result"))

    if best_result is not None and best_score >= SIM_THRESHOLD:
        return best_result
    return None


def set_cached_subagent_response(principal: Principal, domain: str,
                                 question: str, result: SubAgentResult) -> None:
    """Store a sub-agent result in its state partition. No-op on any failure."""
    if not ENABLED or domain in UNCACHED_DOMAINS or not _cacheable(result):
        return
    client = _client()
    if client is None:
        return

    state = extract_state(question)
    pkey = _partition_key(principal, domain, state)
    record = {
        "question": question,
        "embedding": _embed(question),
        "result": asdict(result),
        "ts": time.time(),
    }
    try:
        client.hset(pkey, uuid.uuid4().hex, json.dumps(record))
        client.expire(pkey, TTL_SECONDS)  # refresh TTL on every write
        _trim_partition(client, pkey)
    except Exception:  # noqa: BLE001 -- caching is best-effort
        return


def _trim_partition(client, pkey: str) -> None:
    """Keep only the newest MAX_ENTRIES_PER_PARTITION entries in a partition."""
    try:
        entries = client.hgetall(pkey)
        if len(entries) <= MAX_ENTRIES_PER_PARTITION:
            return
        by_age = sorted(
            entries.items(),
            key=lambda kv: _record_ts(kv[1]),
        )
        stale = [field for field, _ in by_age[:-MAX_ENTRIES_PER_PARTITION]]
        if stale:
            client.hdel(pkey, *stale)
    except Exception:  # noqa: BLE001
        return


def _record_ts(raw: str) -> float:
    try:
        return float(json.loads(raw).get("ts", 0.0))
    except (ValueError, TypeError, AttributeError):
        return 0.0


def _rehydrate(data) -> SubAgentResult | None:
    """Rebuild a SubAgentResult from its stored dict, tolerating schema drift.

    Only known fields are passed through, so an entry written by an older or
    newer build (extra/missing fields) is still usable rather than a crash.
    """
    if not isinstance(data, dict):
        return None
    allowed = SubAgentResult.__dataclass_fields__  # type: ignore[attr-defined]
    known = {k: v for k, v in data.items() if k in allowed}
    try:
        return SubAgentResult(**known)
    except TypeError:
        return None
