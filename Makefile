.PHONY: deps setup deploy destroy pause resume sync poll test-webhook watermark-reset ibkr-flex-dump ibkr-flex-refresh types test typecheck lint e2e e2e-up e2e-run e2e-down local-up local-down logs stats ssh help

PROJECT = relayport
PYTHON ?= .venv/bin/python3
E2E_ENV = .env.test
E2E_COMPOSE = SITE_DOMAIN=unused API_TOKEN=test-token docker compose -f docker-compose.yml -f docker-compose.test.yml -p $(PROJECT)-test --env-file $(E2E_ENV)
E2E_COMPOSE_DOWN = SITE_DOMAIN=unused API_TOKEN=test-token docker compose -f docker-compose.yml -f docker-compose.test.yml -p $(PROJECT)-test --env-file $(E2E_ENV)
LOCAL_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.local.yml
_RESOLVE_ENV = . ./.env 2>/dev/null; . ./.env.droplet 2>/dev/null; env="$${RELAY_ENV:-$${DEFAULT_CLI_ENV:-prod}}"; [ -n "$(ENV)" ] && env="$(ENV)";

define auto_debug_replicas
if [ -f .env ]; then . ./.env; if [ -n "$$(printf '%s' "$${DEBUG_WEBHOOK_PATH:-}" | tr -d '[:space:]')" ]; then export DEBUG_REPLICAS=$${DEBUG_REPLICAS:-1}; fi; fi
endef

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-12s %s\n", $$1, $$2}'

PIP ?= $(dir $(PYTHON))pip
REQ_FILES = -r requirements-dev.txt -r services/relay_core/requirements.txt

deps: ## Install Python dependencies
	$(PIP) install $(REQ_FILES)

setup: ## Create .venv and install all dependencies
	@if [ -d .venv ] && ! $(PYTHON) -c "import pip" >/dev/null 2>&1; then \
		echo "  Existing .venv is broken (shebangs point at a missing interpreter) — rebuilding"; \
		rm -rf .venv; \
	fi
	@test -d .venv || python3 -m venv .venv
	$(MAKE) deps PIP=.venv/bin/pip
	@echo "$(CURDIR)/services/debug" > $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth
	@echo "$(CURDIR)/services" >> $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth
	@echo "$(CURDIR)/services/relay_core" >> $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth
	@for f in env_examples/*; do \
		name=$$(basename "$$f"); \
		[ "$$name" = "env.test" ] && continue; \
		target="./.$${name}"; \
		if [ ! -f "$$target" ]; then \
			cp "$$f" "$$target"; \
			echo "  Created .$${name}"; \
		fi; \
	done
	@[ -f .env.test ] || echo "  NOTE: .env.test not auto-created — cp env_examples/env.test .env.test and set real paper-account values before running E2E tests."

deploy: ## Deploy infrastructure (Terraform + Docker)
	$(PYTHON) -m cli deploy

destroy: ## Permanently destroy all infrastructure
	$(PYTHON) -m cli destroy

pause: ## Snapshot droplet + delete (save costs)
	$(PYTHON) -m cli pause

resume: ## Restore droplet from snapshot
	$(PYTHON) -m cli resume

sync: ## Push .env + restart (S=service B=1 LOCAL_FILES=1 SKIP_E2E=1 ENV=local)
	@$(_RESOLVE_ENV) \
	if [ "$$env" = "local" ]; then \
		$(auto_debug_replicas); \
		$(LOCAL_COMPOSE) up -d --force-recreate $(if $(B),--build); \
	else \
		$(PYTHON) -m cli sync $(S) $(if $(LOCAL_FILES),--local-files) $(if $(B),--build) $(if $(SKIP_E2E),--skip-e2e); \
	fi

poll: ## Trigger an immediate poll (RELAY=ibkr, IDX=1, V=1 verbose, REPLAY=N resend)
	@$(_RESOLVE_ENV) \
	relay="$(RELAY)"; \
	if [ -z "$$relay" ]; then relay=$$(. ./.env 2>/dev/null; echo "$${RELAYS%%,*}"); fi; \
	relay=$${relay:-ibkr}; \
	RELAY_ENV=$$env $(PYTHON) -m cli poll $$relay $(or $(IDX),1) $(if $(V),-v) $(if $(REPLAY),--replay $(REPLAY))

reset-db: ## Drop dedup and meta tables (fresh state) [ENV=local, Y=1 to skip prompt]
	@$(_RESOLVE_ENV) \
	RELAY_ENV=$$env $(PYTHON) -m cli reset-db $(if $(Y),--yes)

test-webhook: ## Send sample trades to webhook endpoint (make test-webhook [S=2] [ENV=local])
	@$(_RESOLVE_ENV) \
	RELAY_ENV=$$env $(PYTHON) -m cli test-webhook $(S)

watermark-reset: ## Reset timestamp watermark to now [RELAY=ibkr or empty for all] [ENV=local]
	@$(_RESOLVE_ENV) \
	RELAY_ENV=$$env $(PYTHON) -m cli watermark-reset $(if $(RELAY),--relay $(RELAY))

ibkr-flex-dump: ## Dump a live IBKR Flex XML response (make ibkr-flex-dump [F=/tmp/raw.xml] [S=_2] [LOOKBACK_DAYS=40])
	@test -f .env.relays || { echo "ERROR: .env.relays not found — create it from env_examples/env.relays"; exit 1; }; \
	set -a; . ./.env.relays; set +a; \
	suffix="$(S)"; \
	days="$(LOOKBACK_DAYS)"; days="$${days:-$$IBKR_FLEX_LOOKBACK_DAYS}"; \
	$(PYTHON) -m relays.ibkr.flex_dump \
		--token "$$(printenv "IBKR_FLEX_TOKEN$$suffix")" \
		--query-id "$$(printenv "IBKR_FLEX_QUERY_ID$$suffix")" \
		$${days:+--lookback-days $$days} \
		$(if $(F),--dump $(F))

ibkr-flex-refresh: ## Refresh IBKR Flex fixture (fetch + auto-detect AF/TC + sanitize) [S=_2] [LOOKBACK_DAYS=40]
	@raw=services/relays/ibkr/fixtures/raw.xml; \
	test -f .env.relays || { echo "ERROR: .env.relays not found — create it from env_examples/env.relays"; exit 1; }; \
	set -a; . ./.env.relays; set +a; \
	suffix="$(S)"; \
	days="$(LOOKBACK_DAYS)"; days="$${days:-$$IBKR_FLEX_LOOKBACK_DAYS}"; \
	$(PYTHON) -m relays.ibkr.flex_dump \
		--token "$$(printenv "IBKR_FLEX_TOKEN$$suffix")" \
		--query-id "$$(printenv "IBKR_FLEX_QUERY_ID$$suffix")" \
		$${days:+--lookback-days $$days} && \
	if grep -q '<TradeConfirm' $$raw; then \
		out=services/relays/ibkr/fixtures/trade_confirm_sample.xml; kind="Trade Confirmation"; \
	else \
		out=services/relays/ibkr/fixtures/activity_flex_sample.xml; kind="Activity Flex"; \
	fi; \
	$(PYTHON) services/relays/ibkr/fixtures/sanitize.py $$raw $$out && rm -f $$raw && \
	echo "Detected $$kind response -> $$out"

types: ## Regenerate TypeScript + Python types from Pydantic models
	PYTHONPATH=services $(PYTHON) schema_gen.py shared > types/typescript/shared/types.schema.json
	npx --yes json-schema-to-typescript types/typescript/shared/types.schema.json > types/typescript/shared/types.d.ts
	PYTHONPATH=services $(PYTHON) schema_gen.py relay_core.relay_models > types/typescript/relay_api/types.schema.json
	npx --yes json-schema-to-typescript types/typescript/relay_api/types.schema.json > types/typescript/relay_api/types.d.ts
	@echo "Generated types/typescript/shared/types.d.ts + types/typescript/relay_api/types.d.ts"
	$(PYTHON) gen_python_types.py
	$(PYTHON) -m ruff check types/python/relayport_types/ --fix --quiet
	$(MAKE) typecheck

test: ## Run unit tests
	PYTHONPATH=.:services:services/relay_core:services/debug $(PYTHON) -m pytest -v

typecheck: ## Run mypy strict type checking
	MYPYPATH=services/relay_core:services $(PYTHON) -m mypy cli/
	MYPYPATH=services $(PYTHON) -m mypy services/shared/
	MYPYPATH=services $(PYTHON) -m mypy services/relay_core/
	MYPYPATH=services $(PYTHON) -m mypy services/relays/
	MYPYPATH=services/debug $(PYTHON) -m mypy services/debug/
	$(PYTHON) -m mypy schema_gen.py
	$(PYTHON) -m mypy gen_python_types.py
	$(PYTHON) -m mypy types/python/relayport_types/

lint: ## Run ruff linter (use FIX=1 to auto-fix)
	$(PYTHON) -m ruff check services/shared/ services/relay_core/ services/relays/ services/debug/ cli/ schema_gen.py gen_python_types.py types/python/relayport_types/ $(if $(FIX),--fix)
	@if grep -rn '__all__' services/ types/ cli/ --include='*.py'; then echo "ERROR: __all__ is banned — use explicit re-exports"; exit 1; fi

local-up: ## Start full stack locally (no TLS, direct port access)
	@$(auto_debug_replicas) && $(LOCAL_COMPOSE) up -d --build
	@echo ""
	@echo "  Relays:   http://localhost:15001/health"
	@if [ -f .env ]; then . ./.env; fi; \
	if [ -n "$$(printf '%s' "$$DEBUG_WEBHOOK_PATH" | tr -d '[:space:]')" ]; then \
		echo "  Debug:    http://localhost:15003/debug/webhook/$$DEBUG_WEBHOOK_PATH"; \
	fi
	@echo ""

local-down: ## Stop local stack
	$(LOCAL_COMPOSE) down

e2e-up: ## Start E2E test stack (relays + debug)
	@test -f $(E2E_ENV) || { echo "ERROR: $(E2E_ENV) not found — cp env_examples/env.test .env.test and set real paper-account values"; exit 1; }
	@if curl -sf http://localhost:15011/health | grep -q '"status": "ok"'; then \
		echo "Stack already running and connected"; \
	else \
		$(E2E_COMPOSE) up -d --build; \
		echo "Waiting for relays..."; \
		relays_ready=false; \
		for i in $$(seq 1 10); do \
			if curl -sf http://localhost:15011/health | grep -q '"status": "ok"'; then \
				relays_ready=true; \
				echo "relays ready"; break; \
			fi; \
			sleep 3; \
		done; \
		if [ "$$relays_ready" != "true" ]; then \
			echo "ERROR: relays did not become healthy within 30s"; \
			exit 1; \
		fi; \
	fi

e2e-down: ## Stop and remove E2E test stack
	$(E2E_COMPOSE_DOWN) down

e2e-run: ## Run E2E tests (stack must be up; recreates containers to pick up .env.test changes)
	@$(E2E_COMPOSE) up -d --force-recreate --wait relays debug > /dev/null 2>&1
	$(PYTHON) -m pytest services/relay_core/tests/e2e/ -v

e2e: ## Run E2E tests (starts/stops stack automatically)
	@test -f $(E2E_ENV) || { echo "ERROR: $(E2E_ENV) not found — cp env_examples/env.test .env.test and set real paper-account values"; exit 1; }
	@was_up=false; \
	if curl -sf http://localhost:15011/health | grep -q '"status": "ok"'; then \
		was_up=true; \
	fi; \
	$(MAKE) e2e-up && $(MAKE) e2e-run; ret=$$?; \
	if [ "$$was_up" = "false" ]; then $(MAKE) e2e-down; fi; \
	exit $$ret

logs: ## Stream logs (S=service ENV=local, default: poller on droplet)
	@$(_RESOLVE_ENV) \
	if [ "$$env" = "local" ]; then \
		$(LOCAL_COMPOSE) logs -f $(or $(S),relays); \
	else \
		[ -n "$$DROPLET_IP" ] || { echo "Error: DROPLET_IP not set — run 'make deploy' first"; exit 1; }; \
		ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP \
			'cd /opt/$(PROJECT) && docker compose logs -f $(or $(S),relays)'; \
	fi

stats: ## Show container resource usage
	@. ./.env 2>/dev/null; . ./.env.droplet 2>/dev/null; \
	[ -n "$$DROPLET_IP" ] || { echo "Error: DROPLET_IP not set — run 'make deploy' first"; exit 1; }; \
	ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP \
		'docker stats --no-stream'

ssh: ## SSH into the droplet
	@. ./.env 2>/dev/null; . ./.env.droplet 2>/dev/null; \
	[ -n "$$DROPLET_IP" ] || { echo "Error: DROPLET_IP not set — run 'make deploy' first"; exit 1; }; \
	ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP

