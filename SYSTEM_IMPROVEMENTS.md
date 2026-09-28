# System Design Improvements

This document outlines the recent architectural and design changes made to the PROOF operations copilot, explaining the rationale behind each decision and the resulting impact on the system and user experience.

---

## 1. Server-Sent Events (SSE) Streaming
**The Problem:** The multi-agent architecture (where a Coordinator delegates to Production, Inventory, Quality, etc.) is extremely thorough but inherently slow. Previously, the backend waited for all agents to finish their LLM generation and tool executions before returning a massive JSON response. Users stared at a loading spinner for up to a minute, often assuming the system had crashed.

**The Change:** We refactored the FastAPI `/api/ask` endpoint to use `StreamingResponse` backed by an internal `EventStreamer`. 
**The Result:** The frontend now receives real-time event chunks (`delegate_start`, `tool_call`, `final`). The React UI updates dynamically—just like ChatGPT or Claude—showing exactly which agent is working and what tool they are calling at any given second. This massively improved perceived latency and operator trust.

## 2. Intent-Partitioned Semantic Caching
**The Problem:** A cache over sub-agent delegations has to satisfy two demands that pull in opposite directions, and the earlier versions each satisfied only one.

1. **Fuzzy matching missed too much.** A first cut used `difflib.SequenceMatcher` at a `0.85` threshold. The coordinator rephrases the same question non-deterministically ("How much scrap at TOR1" → "For plant TOR1, what is the scrap…"), the score dropped to ~0.43, and the cache consistently missed.
2. **Lowering the threshold was dangerous.** Relaxing to `0.65`/`0.40` to catch rephrasings made "scrap at TOR1" and "scrap at TOR2" — one character apart — score ~0.95 and collide. A too-forgiving matcher serves TOR1's data for a TOR2 question. That is the "white shirts under 100" vs "black shirts under 100" problem: a filter changed, but the strings look almost identical.
3. **The exact-string fallback that replaced it had two correctness holes of its own.** The key was `agent_cache:{domain}:{question}` — where `question` was the coordinator's **top-level** question, not the sub-question actually sent to the domain agent, so two different sub-questions to the same domain in one turn collided. More seriously, **the principal was not in the key at all.** A result produced for a plant manager could be served to a shift supervisor or a user scoped to a different plant — silently undoing the whole Phase 4 identity model, whose entire claim is that authority is a property of *who is asking*.

**The Change:** A two-stage lookup that keeps the filters exact and lets semantics handle only wording (`proof/agents/cache.py`).

- **Stage 1 — state partition (exact).** A lightweight extractor pulls the *hard filters* out of the sub-question: plant/line/run/PO/SO/SKU identifiers, numeric thresholds, and product category (sweet-goods / flatbread / artisan). Those, together with the resolved **principal** and the **domain**, form the partition key. If any filter differs, it is a different partition — so TOR1 vs TOR2, line 3 vs line 5, and sweet-goods vs flatbread can never collide, and one principal's entry is never even a candidate for another.
- **Stage 2 — semantic match (fuzzy), inside the partition only.** Within a partition, the query is embedded with the same offline all-MiniLM model used for procedure retrieval (no new egress) and compared by cosine to previously-cached questions. A hit requires similarity ≥ `0.65` — calibrated so genuine paraphrases (~0.75–0.85) hit while two different intents that happen to share a partition (~0.35–0.47) do not. Nearest-wins, so each intent retrieves its own answer.

Three further correctness properties were added at the same time: the cache **fails open** (any Redis or embedding error skips the cache rather than breaking the answer), entries carry a **TTL** (default 15 min) so stale plant state is not served forever, and **denials, empty answers, and fabricated-citation answers are never cached**.

**The Result:** Repeated and rephrased questions hit the cache (milliseconds, ~$0), while a changed filter or a different operator is a guaranteed miss. The behaviour is pinned by `make smoke-cache` — 15 assertions, no API key and no running Redis required — including the two that matter most: *the same question from a different principal misses*, and *a changed filter never collides*.

## 3. Explicit Agent Reasoning (Thought Extraction)
**The Problem:** In our efforts to make the system transparent, we wanted the UI to display the *reasoning* behind every tool call. However, certain testing models (like the mock `gpt-5.6-luna`) were highly optimized to emit JSON `tool_call` blocks immediately, bypassing standard text generation. The UI reasoning boxes remained empty.

**The Change:** We updated the `SUBAGENT_SYSTEM` prompt to include a strict directive: `- You MUST briefly explain your reasoning in text before making any tool call.` We then updated the streamer to intercept this preceding text block and attach it to the tool call event as an `explanation`.
**The Result:** Operators can now read the exact logical steps the AI took *before* it queried the MES or ERP databases, ensuring full traceability of AI decisions.

## 4. ExceptionGroup Unwrapping
**The Problem:** Because the Coordinator uses Python 3.12+ `asyncio.TaskGroup` to run sub-agents concurrently, any API or gateway errors were bundled into an `ExceptionGroup`. If a model gateway denied access, the UI displayed a massive, intimidating Python stack trace (`ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)...`), violating the requirement for a professional interface.

**The Change:** We implemented a recursive error-unwrapping utility in the FastAPI global exception handler (`app.py`). 
**The Result:** The UI now receives a clean, single-line actionable error message (e.g., "Permission Denied by Gateway"), while the backend logs retain the full tracebacks for engineering debugging.

## 5. UI State Preservation on Identity Switch
**The Problem:** The security model uses the `X-Proof-Principal` header to enforce database-level row permissions. When a user changed their role in the UI dropdown (e.g., from Shift Supervisor to Plant Manager) to approve a pending action, the React `useEffect` hook completely wiped the `messages` array to fetch the new role's pending tasks, destroying the user's chat history.

**The Change:** We modified the chat rendering logic to safely append to the state instead of resetting it. 
**The Result:** Switching identities now safely preserves the entire conversation history. It simply appends a system notification (`Active identity changed`) and loads any relevant pending actions at the bottom of the feed, allowing seamless Human-In-The-Loop (HITL) approvals without losing workflow context.
