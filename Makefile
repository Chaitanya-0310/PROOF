# PROOF -- one command per phase.
#
# First run:
#   cp .env.example .env
#   make up        # start postgres, bootstrap schemas and roles
#   make install   # host python env (uv, 3.12)
#   make seed      # generate 90 days of synthetic plant history
#   make scenario  # plant the three reproducible demo situations
#   make verify    # sanity-check the dataset
#
# Split of responsibilities:
#   Docker runs POSTGRES ONLY. Application code runs on the host under uv.
#   Rebuilding an image on every dependency change -- and paying Windows
#   bind-mount latency on every edit -- costs more than it buys when the
#   only thing that genuinely needs isolating is the database.
#   The `app` service stays in docker-compose.yml as a no-install fallback
#   for anyone cloning this who would rather not have Python locally:
#   `docker compose exec -T app python seed/generate_plant.py`.

COMPOSE = docker compose
PY      = uv run python

.PHONY: help up down reset logs psql shell install seed scenario verify
.PHONY: smoke smoke-mcp smoke-cache bench-router smoke-rag smoke-authz index ask demo1 demo2 demo3
.PHONY: pending approve reject smoke-trace eval-check eval
.PHONY: baseline baseline-live

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up: ## Start containers; first boot runs db/init (schemas + least-privilege roles)
	$(COMPOSE) up -d --build
	@echo "Waiting for db healthcheck..."
	@$(COMPOSE) exec -T db sh -c 'until pg_isready -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-proof}; do sleep 1; done'
	@echo "Up. Next: make seed"

down: ## Stop containers (keeps the data volume)
	$(COMPOSE) down

reset: ## DESTROY the volume and rebuild from scratch (re-runs db/init)
	$(COMPOSE) down -v
	$(MAKE) up

logs: ## Tail db logs
	$(COMPOSE) logs -f db

psql: ## Superuser psql shell
	$(COMPOSE) exec db psql -U $${POSTGRES_SUPERUSER:-postgres} -d $${POSTGRES_DB:-proof}

shell: ## Bash shell inside the app container
	$(COMPOSE) exec app bash

install: ## Create the host python 3.12 env and install requirements
	uv venv --python 3.12
	uv pip install -r requirements.txt

seed: ## Phase 1 -- generate 90 days of synthetic plant history
	$(PY) seed/generate_plant.py

scenario: ## Phase 1 -- plant the three reproducible demo situations
	$(PY) seed/scenarios.py

verify: ## Phase 1 -- sanity-check the generated dataset
	$(PY) seed/verify.py

# --- Phase 2 -------------------------------------------------------------
# The two smoke targets need NO API key. They prove the tool layer is correct
# and the MCP servers speak the protocol, before any model is involved --
# debugging a wrong number and debugging a wrong tool choice are different
# jobs, and doing both at once is how these projects stall.

smoke: ## Phase 2 -- exercise every domain tool against the DB (no API key)
	$(PY) scripts/smoke_tools.py

smoke-mcp: ## Phase 2 -- start each MCP server and call it over stdio (no API key)
	$(PY) scripts/smoke_mcp.py

smoke-cache: ## Semantic cache -- identity + filter partitioning (no API key, no Redis)
	$(PY) scripts/smoke_cache.py

ARMS ?= off,light
REPS ?= 2
bench-router: ## Router benchmark, graded: make bench-router [ARMS=off,light,jev] [REPS=2] (needs key)
	$(PY) scripts/bench_router.py --arms $(ARMS) --reps $(REPS)

# --- Phase 3 -------------------------------------------------------------

index: ## Phase 3 -- parse the SOP corpus and build the hybrid index
	$(PY) proof/rag/index.py

smoke-rag: ## Phase 3 -- retrieval eval + citation enforcement (no API key)
	$(PY) scripts/smoke_rag.py

smoke-authz: ## Phase 4 -- identity, authz and the approval queue (no API key)
	$(PY) scripts/smoke_authz.py

# --- Phase 4: the human side ---------------------------------------------
# A SEPARATE program from the agent, connecting as a SEPARATE database role.
# `approver` is the only role holding UPDATE on act.proposed_actions, so
# "the agent approved its own proposal" would need a new grant, not a new prompt.

pending: ## Phase 4 -- list actions awaiting human approval
	$(PY) -m proof.approve list $(if $(PLANT),--plant $(PLANT),)

approve: ## Phase 4 -- approve an action:  make approve ID=3 AS=j.okafor NOTE="..."
	$(PY) -m proof.approve approve $(ID) --as $(AS) $(if $(NOTE),--note "$(NOTE)",)

reject: ## Phase 4 -- reject an action:  make reject ID=3 AS=j.okafor NOTE="..."
	$(PY) -m proof.approve reject $(ID) --as $(AS) $(if $(NOTE),--note "$(NOTE)",)

# --- Phase 5: observability + evals --------------------------------------

smoke-trace: ## Phase 5 -- trace tree + cost rollup, offline (no API key)
	$(PY) scripts/smoke_trace.py

eval-check: ## Phase 5 -- prove the graders discriminate, offline (no API key)
	$(PY) -m evals.run --check-graders

eval: ## Phase 5 -- run the golden set through the agent (needs a key)
	$(PY) -m evals.run $(if $(CASE),--case $(CASE),)

# --- Phase 6: baseline harness ----------------------------------------
# The offline target needs NO API key -- it computes scrap-avoided and
# time-to-decision from the planted scenarios using the database alone.
# The live target runs the coordinator and measures real agent response time.

baseline: ## Phase 6 -- scrap avoided + time-to-decision, offline (no API key)
	$(PY) scripts/run_baseline.py

baseline-live: ## Phase 6 -- same, but runs the agent to measure real time (needs a key)
	$(PY) scripts/run_baseline.py --live

# --- Frontend: operator console (React SPA + FastAPI) --------------------
# Run these in two terminals: `make api` then `make web`, and open :5173.
# Dashboard and Approvals work with no LLM key; Ask lights up once one is set.

api: ## Frontend -- run the FastAPI backend on :8000
	uv run uvicorn proof.api.app:app --reload --port 8000

web-install: ## Frontend -- install the React app's npm deps (once)
	cd web && npm install

web: ## Frontend -- run the React dev server on :5173 (proxies /api to :8000)
	cd web && npm run dev

ask: ## Phase 2 -- ask the coordinator:  make ask Q="..." [AS=j.okafor]
	$(PY) -m proof.cli $(if $(V),-v,) $(if $(AS),--as $(AS),) "$(Q)"

demo1: ## Phase 2 -- the hero demo: line down, what is at risk
	$(PY) -m proof.cli -v demo1

demo2: ## Phase 2 -- supplier slip: what does a late flour truck break
	$(PY) -m proof.cli -v demo2

demo3: ## Phase 2 -- root cause: why is sweet-goods scrap up
	$(PY) -m proof.cli -v demo3
