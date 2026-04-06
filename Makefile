.PHONY: setup deploy destroy pause resume sync order poll poll2 test-webhook types test typecheck lint e2e e2e-up e2e-run e2e-down local-up local-down logs stats gateway ssh help

PROJECT = ibkr-relay
PYTHON ?= .venv/bin/python3
E2E_ENV = .env.test
E2E_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.test.yml -p $(PROJECT)-test --env-file $(E2E_ENV)
LOCAL_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.local.yml
CLI_RELAY_ENV = $(if $(ENV),RELAY_ENV=$(ENV))

# Allow disabling poller: make local-up POLLER=0  or  make sync POLLER=0
ifdef POLLER
  ifneq ($(filter $(POLLER),0 1),$(POLLER))
    $(error POLLER must be 0 or 1 (got: $(POLLER)))
  endif
  export POLLER_REPLICAS := $(POLLER)
endif

# Allow disabling gateway stack: make local-up REMOTE_CLIENT=0  or  make sync REMOTE_CLIENT=0
ifdef REMOTE_CLIENT
  ifneq ($(filter $(REMOTE_CLIENT),0 1),$(REMOTE_CLIENT))
    $(error REMOTE_CLIENT must be 0 or 1 (got: $(REMOTE_CLIENT)))
  endif
  export GATEWAY_REPLICAS := $(REMOTE_CLIENT)
endif

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-12s %s\n", $$1, $$2}'

setup: ## Create .venv and install all dependencies
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt -r services/poller/requirements.txt -r services/remote-client/requirements.txt
	@echo "$(CURDIR)/services/poller" > $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth
	@echo "$(CURDIR)/services/remote-client" >> $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth
	@echo "$(CURDIR)/services" >> $$(find .venv/lib -name site-packages -type d)/$(PROJECT).pth

deploy: ## Deploy infrastructure (Terraform + Docker)
	$(PYTHON) -m cli deploy

destroy: ## Permanently destroy all infrastructure
	$(PYTHON) -m cli destroy

pause: ## Snapshot droplet + delete (save costs)
	$(PYTHON) -m cli pause

resume: ## Restore droplet from snapshot
	$(PYTHON) -m cli resume

sync: ## Push .env + restart (S=gateway B=1 LOCAL_FILES=1 SKIP_E2E=1)
	$(PYTHON) -m cli sync $(S) $(if $(LOCAL_FILES),--local-files) $(if $(B),--build) $(if $(SKIP_E2E),--skip-e2e)

order: ## Place a stock order (e.g. make order Q=2 SYM=TSLA T=MKT [P=] [CUR=EUR] [EX=LSE] [TIF=GTC] [RTH=1] [ENV=local])
	@. ./.env && \
		rc="$${REMOTE_CLIENT_ENABLED:-true}"; \
		if [ "$$rc" = "false" ] || [ "$$rc" = "0" ] || [ "$$rc" = "no" ] || [ -z "$$rc" ]; then \
			echo "ERROR: REMOTE_CLIENT_ENABLED is false — order API requires the gateway stack" >&2; exit 1; \
		fi
	$(CLI_RELAY_ENV) $(PYTHON) -m cli order $(Q) $(SYM) $(T) $(P) $(CUR) $(EX) $(if $(TIF),--tif $(TIF)) $(if $(RTH),--outside-rth)

poll: ## Trigger an immediate Flex poll (V=1 verbose, DEBUG=1 raw XML, REPLAY=N resend, ENV=local)
	$(CLI_RELAY_ENV) $(PYTHON) -m cli poll $(if $(V),-v) $(if $(DEBUG),--debug) $(if $(REPLAY),--replay $(REPLAY))

poll2: ## Trigger an immediate Flex poll (second poller, ENV=local)
	$(CLI_RELAY_ENV) $(PYTHON) -m cli poll 2 $(if $(V),-v) $(if $(DEBUG),--debug) $(if $(REPLAY),--replay $(REPLAY))

test-webhook: ## Send sample trades to webhook endpoint (make test-webhook [S=2] [ENV=local])
	$(CLI_RELAY_ENV) $(PYTHON) -m cli test-webhook $(S)

types: ## Regenerate TypeScript types from Pydantic models
	PYTHONPATH=services/poller:services/remote-client:services $(PYTHON) schema_gen.py models_poller > types/poller/types.schema.json
	npx --yes json-schema-to-typescript types/poller/types.schema.json > types/poller/types.d.ts
	PYTHONPATH=services/poller:services/remote-client:services $(PYTHON) schema_gen.py models_remote_client > types/http/types.schema.json
	npx --yes json-schema-to-typescript types/http/types.schema.json > types/http/types.d.ts
	@echo "Generated types/poller/types.d.ts + types/http/types.d.ts"

test: ## Run unit tests
	PYTHONPATH=.:services/poller:services/remote-client:services $(PYTHON) -m pytest -v

typecheck: ## Run mypy strict type checking
	MYPYPATH=services/poller:services $(PYTHON) -m mypy services/poller/ cli/test_webhook.py
	MYPYPATH=services/remote-client:services/poller:services $(PYTHON) -m mypy services/remote-client/
	MYPYPATH=services $(PYTHON) -m mypy services/notifier/
	$(PYTHON) -m mypy schema_gen.py

lint: ## Run ruff linter (use FIX=1 to auto-fix)
	$(PYTHON) -m ruff check services/poller/ services/remote-client/ services/notifier/ cli/ schema_gen.py $(if $(FIX),--fix)

local-up: ## Start full stack locally (no TLS, direct port access)
	@if [ -f .env ]; then \
		. ./.env; \
		rc="$${REMOTE_CLIENT_ENABLED:-true}"; \
		if [ "$$rc" = "false" ] || [ "$$rc" = "0" ] || [ "$$rc" = "no" ] || [ -z "$$rc" ]; then \
			export GATEWAY_REPLICAS=$${GATEWAY_REPLICAS:-0}; \
		fi; \
		pe="$${POLLER_ENABLED:-true}"; \
		if [ "$$pe" = "false" ] || [ "$$pe" = "0" ] || [ "$$pe" = "no" ] || [ -z "$$pe" ]; then \
			export POLLER_REPLICAS=$${POLLER_REPLICAS:-0}; \
		fi; \
	fi && \
	$(LOCAL_COMPOSE) up -d --build
	@echo ""
	@echo "  REST API: http://localhost:15000/health"
	@echo "  Poller:   http://localhost:15001/health"
	@echo "  VNC:      http://localhost:15002"
	@echo ""

local-down: ## Stop local stack
	$(LOCAL_COMPOSE) down

e2e-up: ## Start E2E test stack (IB Gateway + remote-client + poller)
	@if curl -sf http://localhost:15010/health | grep -q '"connected": true' && \
	    curl -sf http://localhost:15011/health | grep -q '"status": "ok"'; then \
		echo "Stack already running and connected"; \
	else \
		$(E2E_COMPOSE) up -d --build; \
		echo "Waiting for remote-client to connect to IB Gateway..."; \
		for i in $$(seq 1 12); do \
			if curl -sf http://localhost:15010/health | grep -q '"connected": true'; then \
				echo "remote-client ready"; break; \
			fi; \
			if $(E2E_COMPOSE) logs ib-gateway 2>&1 | grep -q "Existing session detected"; then \
				echo ""; \
				echo "ERROR: IB Gateway detected an existing session (another login is active)."; \
				echo "This is likely the production droplet or another local stack."; \
				echo "Disconnect that session first, then:  make e2e-down && make e2e-up"; \
				echo ""; \
				exit 1; \
			fi; \
			if ! $(E2E_COMPOSE) ps ib-gateway --status running -q 2>/dev/null | grep -q .; then \
				echo ""; \
				echo "ERROR: ib-gateway container exited unexpectedly."; \
				echo "Last logs:"; \
				$(E2E_COMPOSE) logs --tail=20 ib-gateway; \
				echo ""; \
				exit 1; \
			fi; \
			sleep 10; \
		done; \
		echo "Waiting for poller..."; \
		for i in $$(seq 1 10); do \
			if curl -sf http://localhost:15011/health | grep -q '"status": "ok"'; then \
				echo "poller ready"; break; \
			fi; \
			sleep 3; \
		done; \
	fi

e2e-down: ## Stop and remove E2E test stack
	$(E2E_COMPOSE) down

e2e-run: ## Run E2E tests (stack must be up)
	@$(E2E_COMPOSE) restart remote-client poller > /dev/null 2>&1 && sleep 3
	$(PYTHON) -m pytest services/remote-client/tests/e2e/ services/poller/tests/e2e/ -v

e2e: ## Run E2E tests against local paper account (starts/stops stack)
	@was_up=false; \
	if curl -sf http://localhost:15010/health | grep -q '"connected": true' && \
	   curl -sf http://localhost:15011/health | grep -q '"status": "ok"'; then \
		was_up=true; \
	fi; \
	$(MAKE) e2e-up && $(MAKE) e2e-run; ret=$$?; \
	if [ "$$was_up" = "false" ]; then $(MAKE) e2e-down; fi; \
	exit $$ret

logs: ## Stream poller logs (Ctrl+C to stop)
	@. ./.env && ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP \
		'cd /opt/$(PROJECT) && docker compose logs -f $(or $(S),poller)'

stats: ## Show container resource usage
	@. ./.env && ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP \
		'docker stats --no-stream'

gateway: ## Start IB Gateway container (then open VNC for 2FA)
	@. ./.env && \
		rc="$${REMOTE_CLIENT_ENABLED:-true}"; \
		if [ "$$rc" = "false" ] || [ "$$rc" = "0" ] || [ "$$rc" = "no" ] || [ -z "$$rc" ]; then \
			echo "ERROR: REMOTE_CLIENT_ENABLED is false — gateway stack is disabled" >&2; exit 1; \
		fi && \
		ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP \
		'cd /opt/$(PROJECT) && docker compose up -d ib-gateway && sleep 2 && docker compose ps ib-gateway'

ssh: ## SSH into the droplet
	@. ./.env && ssh -i $${SSH_KEY:-$$HOME/.ssh/$(PROJECT)} root@$$DROPLET_IP
