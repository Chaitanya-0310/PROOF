# System Design Improvements

This document outlines the recent architectural and design changes made to the PROOF operations copilot, explaining the rationale behind each decision and the resulting impact on the system and user experience.

---

## 1. Server-Sent Events (SSE) Streaming
**The Problem:** The multi-agent architecture (where a Coordinator delegates to Production, Inventory, Quality, etc.) is extremely thorough but inherently slow. Previously, the backend waited for all agents to finish their LLM generation and tool executions before returning a massive JSON response. Users stared at a loading spinner for up to a minute, often assuming the system had crashed.

**The Change:** We refactored the FastAPI `/api/ask` endpoint to use `StreamingResponse` backed by an internal `EventStreamer`. 
**The Result:** The frontend now receives real-time event chunks (`delegate_start`, `tool_call`, `final`). The React UI updates dynamically—just like ChatGPT or Claude—showing exactly which agent is working and what tool they are calling at any given second. This massively improved perceived latency and operator trust.

## 2. Refactoring Semantic Caching (Fuzzy to Exact-Match)
**The Problem:** We implemented a semantic cache using Python's fuzzy string matching (`difflib.SequenceMatcher`) to intercept sub-agent queries with an initial similarity threshold of `0.85`. However, it failed in two ways:
1. **Unpredictable Misses:** The Coordinator LLM non-deterministically rephrases user questions. "How much scrap at TOR1" became "For plant TOR1, what is the scrap..." on the first run, and "At plant TOR1, list the scrap..." on the second. The search score dropped to `0.43` (43%), falling well below the `0.85` threshold and causing the cache to consistently miss.
2. **Dangerous False Positives:** We experimented with lowering the threshold to `0.65` or even `0.40` to catch those variations, but doing so introduced a massive risk. A fuzzy match might see "What is the scrap at TOR1" and "What is the scrap at TOR2" as having a similarity score of 95% (because only one character differs). If the threshold is too forgiving, the system risks serving TOR1's data when asked about TOR2.

**The Change:** We moved the cache lookup to rely on the **user's exact original string** (`agent_cache:domain:question.strip().lower()`) rather than the LLM's generated sub-question, completely bypassing the `SequenceMatcher` and its thresholds.
**The Result:** A strict hash lookup guarantees a 100% cache hit when an operator repeats a question, securely preventing plant-name confusion. This drops latency to milliseconds and reduces LLM API costs to zero for repeated queries, while preserving all tool metadata for the UI to display.

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
