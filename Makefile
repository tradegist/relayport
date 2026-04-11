.PHONY: setup deploy destroy pause resume sync poll poll2 test-webhook types test typecheck lint e2e e2e-up e2e-run e2e-down local-up local-down logs stats ssh help

PROJECT = ibkr-relay
PYTHON ?= .venv/bin/python3
E2E_ENV = .env.test
E2E_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.test.yml -p $(PROJECT)-test --env-file $(E2E_ENV)
E2E_COMPOSE_DOWN = docker compose -f docker-compose.yml -f docker-compose.test.yml -p $(PROJECT)-test --env-file $(E2E_ENV)
LOCAL_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.local.yml
CLI_RELAY_ENV = $(if $(ENV),RELAY_ENV=$(ENV))

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-12s %s\n", $$1, $$2}'

setup: ## Create .venv and install all dependencies
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt -r services/poller/requirements.txt
	@echo "$(CURDIR)/services/poller" > $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth
	@echo "$(CURDIR)/services/debug" >> $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth
	@echo "$(CURDIR)/services" >> $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth
	@echo "$(CURDIR)/services/relay_core" >> $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth

deploy: ## Deploy infrastructure (Terraform + Docker)
	$(PYTHON) -m cli deploy

destroy: ## Permanently destroy all infrastructure
	$(PYTHON) -m cli destroy

pause: ## Snapshot droplet + delete (save costs)
	$(PYTHON) -m cli pause

resume: ## Restore droplet from snapshot
	$(PYTHON) -m cli resume

sync: ## Push .env + restart (S=service B=1 LOCAL_FILES=1 SKIP_E2E=1 ENV=local)
	@. ./.env 2>/dev/null; \
	env="$${RELAY_ENV:-$${DEFAULT_CLI_RELAY_ENV:-prod}}"; \
	[ -n "$(ENV)" ] && env="$(ENV)"; \
	if [ "$$env" = "local" ]; then \
		$(LOCAL_COMPOSE) restart; \
	else \
		$(PYTHON) -m cli sync $(S) $(if $(LOCAL_FILES),--local-files) $(if $(B),--build) $(if $(SKIP_E2E),--skip-e2e); \
	fi

poll: ## Trigger an immediate poll (RELAY=ibkr, IDX=1, V=1 verbose, DEBUG=1 raw XML, REPLAY=N resend)
	$(CLI_RELAY_ENV) $(PYTHON) -m cli poll $(or $(RELAY),ibkr) $(or $(IDX),1) $(if $(V),-v) $(if $(DEBUG),--debug) $(if $(REPLAY),--replay $(REPLAY))

test-webhook: ## Send sample trades to webhook endpoint (make test-webhook [S=2] [ENV=local])
	$(CLI_RELAY_ENV) $(PYTHON) -m cli test-webhook $(S)

types: ## Regenerate TypeScript + Python types from Pydantic models
	PYTHONPATH=services $(PYTHON) schema_gen.py shared > types/typescript/shared/types.schema.json
	npx --yes json-schema-to-typescript types/typescript/shared/types.schema.json > types/typescript/shared/types.d.ts
	PYTHONPATH=services/poller:services $(PYTHON) schema_gen.py poller_models > types/typescript/poller/types.schema.json
	npx --yes json-schema-to-typescript types/typescript/poller/types.schema.json > types/typescript/poller/types.d.ts
	@echo "Generated types/typescript/shared/types.d.ts + types/typescript/poller/types.d.ts"
	$(PYTHON) gen_python_types.py

test: ## Run unit tests
	PYTHONPATH=.:services/poller:services:services/debug $(PYTHON) -m pytest -v

typecheck: ## Run mypy strict type checking
	MYPYPATH=services/poller:services $(PYTHON) -m mypy services/poller/ cli/test_webhook.py
	MYPYPATH=services $(PYTHON) -m mypy services/notifier/
	MYPYPATH=services $(PYTHON) -m mypy services/dedup/
	MYPYPATH=services $(PYTHON) -m mypy services/shared/
	MYPYPATH=services $(PYTHON) -m mypy services/listener/
	MYPYPATH=services $(PYTHON) -m mypy services/relay_core/
	MYPYPATH=services/poller:services $(PYTHON) -m mypy services/relays/
	MYPYPATH=services/debug $(PYTHON) -m mypy services/debug/
	$(PYTHON) -m mypy schema_gen.py
	$(PYTHON) -m mypy gen_python_types.py
	$(PYTHON) -m mypy types/python/ibkr_relay_types/

lint: ## Run ruff linter (use FIX=1 to auto-fix)
	$(PYTHON) -m ruff check services/poller/ services/notifier/ services/dedup/ services/shared/ services/listener/ services/relay_core/ services/relays/ services/debug/ cli/ schema_gen.py gen_python_types.py types/python/ibkr_relay_types/ $(if $(FIX),--fix)
	@if grep -rn '__all__' services/ types/ cli/ --include='*.py'; then echo "ERROR: __all__ is banned — use explicit re-exports"; exit 1; fi

local-up: ## Start full stack locally (no TLS, direct port access)
	@if [ -f .env ]; then \
		. ./.env; \
		debug_webhook_path="$${DEBUG_WEBHOOK_PATH:-}"; \
		if [ -n "$$(printf '%s' "$$debug_webhook_path" | tr -d '[:space:]')" ]; then \
			export DEBUG_REPLICAS=$${DEBUG_REPLICAS:-1}; \
		fi; \
	fi && \
	$(LOCAL_COMPOSE) up -d --build
	@echo ""
	@echo "  Relays:   http://localhost:15001/health"
	@if [ -f .env ]; then . ./.env; fi; \
	if [ -n "$$(printf '%s' "$$DEBUG_WEBHOOK_PATH" | tr -d '[:space:]')" ]; then \
		echo "  Debug:    http://localhost:15003/debug/webhook/$$DEBUG_WEBHOOK_PATH"; \
	fi
	@echo ""

local-down: ## Stop local stack
	$(LOCAL_COMPOSE) down

e2e-up: ## Start E2E test stack (relays + ibkr-debug)
	@test -f $(E2E_ENV) || { echo "ERROR: $(E2E_ENV) not found — run: cp .env.test.example .env.test (placeholder values are fine)"; exit 1; }
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

e2e-run: ## Run E2E tests (stack must be up)
	@$(E2E_COMPOSE) restart relays ibkr-debug > /dev/null 2>&1 && sleep 3
	$(PYTHON) -m pytest services/poller/tests/e2e/ services/listener/tests/e2e/ -v

e2e: ## Run E2E tests (starts/stops stack automatically)
	@test -f $(E2E_ENV) || { echo "ERROR: $(E2E_ENV) not found — run: cp .env.test.example .env.test (placeholder values are fine)"; exit 1; }
	@was_up=false; \
	if curl -sf http://localhost:15011/health | grep -q '"status": "ok"'; then \
		was_up=true; \
	fi; \
	$(MAKE) e2e-up && $(MAKE) e2e-run; ret=$$?; \
	if [ "$$was_up" = "false" ]; then $(MAKE) e2e-down; fi; \
	exit $$ret

logs: ## Stream logs (S=service ENV=local, default: poller on droplet)
	@. ./.env && \
	env="$${RELAY_ENV:-$${DEFAULT_CLI_RELAY_ENV:-prod}}"; \
	[ -n "$(ENV)" ] && env="$(ENV)"; \
	if [ "$$env" = "local" ]; then \
		$(LOCAL_COMPOSE) logs -f $(or $(S),relays); \
	else \
		ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP \
			'cd /opt/$(PROJECT) && docker compose logs -f $(or $(S),relays)'; \
	fi

stats: ## Show container resource usage
	@. ./.env && ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP \
		'docker stats --no-stream'

ssh: ## SSH into the droplet
	@. ./.env && ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP

