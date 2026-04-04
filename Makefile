.PHONY: setup deploy destroy pause resume sync order poll poll2 test-webhook types test typecheck e2e e2e-up e2e-run e2e-down logs stats gateway ssh help

PYTHON = .venv/bin/python3

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-12s %s\n", $$1, $$2}'

setup: ## Create .venv and install all dependencies
	@test -d .venv || python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt -r poller/requirements.txt -r remote-client/requirements.txt

deploy: ## Deploy infrastructure (Terraform + Docker)
	$(PYTHON) -m cli deploy

destroy: ## Permanently destroy all infrastructure
	$(PYTHON) -m cli destroy

pause: ## Snapshot droplet + delete (save costs)
	$(PYTHON) -m cli pause

resume: ## Restore droplet from snapshot
	$(PYTHON) -m cli resume

sync: ## Push .env + restart all services (or: make sync S=gateway)
	$(PYTHON) -m cli sync $(S)

order: ## Place a stock order (e.g. make order Q=2 SYM=TSLA T=MKT [P=] [CUR=EUR] [EX=LSE] [TIF=GTC] [RTH=1])
	$(PYTHON) -m cli order $(Q) $(SYM) $(T) $(P) $(CUR) $(EX) $(if $(TIF),--tif $(TIF)) $(if $(RTH),--outside-rth)

poll: ## Trigger an immediate Flex poll (V=1 verbose, DEBUG=1 raw XML, REPLAY=N resend)
	$(PYTHON) -m cli poll $(if $(V),-v) $(if $(DEBUG),--debug) $(if $(REPLAY),--replay $(REPLAY))

poll2: ## Trigger an immediate Flex poll (second poller)
	$(PYTHON) -m cli poll 2 $(if $(V),-v) $(if $(DEBUG),--debug) $(if $(REPLAY),--replay $(REPLAY))

test-webhook: ## Send sample trades to webhook endpoint (make test-webhook [S=2])
	$(PYTHON) -m cli test-webhook $(S)

types: ## Regenerate TypeScript types from Pydantic models
	$(PYTHON) poller/models.py > types/poller/webhook.schema.json
	npx --yes json-schema-to-typescript types/poller/webhook.schema.json > types/poller/webhook.d.ts
	$(PYTHON) remote-client/models.py > types/http/order.schema.json
	npx --yes json-schema-to-typescript types/http/order.schema.json > types/http/order.d.ts
	@echo "Generated types/poller/webhook.d.ts + types/http/order.d.ts"

test: ## Run unit tests
	PYTHONPATH=.:poller $(PYTHON) -m pytest -v

typecheck: ## Run mypy strict type checking
	MYPYPATH=poller $(PYTHON) -m mypy poller/ cli/test_webhook.py
	MYPYPATH=remote-client $(PYTHON) -m mypy remote-client/

E2E_ENV = .env.test
E2E_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.test.yml -p ibkr-relay-test --env-file $(E2E_ENV)

e2e-up: ## Start E2E test stack (IB Gateway + webhook-relay + poller)
	@if curl -sf http://localhost:15000/health | grep -q '"connected": true' && \
	    curl -sf http://localhost:15001/health | grep -q '"status": "ok"'; then \
		echo "Stack already running and connected"; \
	else \
		$(E2E_COMPOSE) up -d --build; \
		echo "Waiting for webhook-relay to connect to IB Gateway..."; \
		for i in $$(seq 1 12); do \
			if curl -sf http://localhost:15000/health | grep -q '"connected": true'; then \
				echo "webhook-relay ready"; break; \
			fi; \
			sleep 10; \
		done; \
		echo "Waiting for poller..."; \
		for i in $$(seq 1 10); do \
			if curl -sf http://localhost:15001/health | grep -q '"status": "ok"'; then \
				echo "poller ready"; break; \
			fi; \
			sleep 3; \
		done; \
	fi

e2e-down: ## Stop and remove E2E test stack
	$(E2E_COMPOSE) down

e2e-run: ## Run E2E tests (stack must be up)
	$(PYTHON) -m pytest remote-client/tests/e2e/ poller/tests/e2e/ -v

e2e: ## Run E2E tests against local paper account (starts/stops stack)
	@was_up=false; \
	if curl -sf http://localhost:15000/health | grep -q '"connected": true' && \
	   curl -sf http://localhost:15001/health | grep -q '"status": "ok"'; then \
		was_up=true; \
	fi; \
	$(MAKE) e2e-up && $(MAKE) e2e-run; ret=$$?; \
	if [ "$$was_up" = "false" ]; then $(MAKE) e2e-down; fi; \
	exit $$ret

logs: ## Stream poller logs (Ctrl+C to stop)
	@. ./.env && ssh -i $${SSH_KEY:-$$HOME/.ssh/ibkr-relay} root@$$DROPLET_IP \
		'cd /opt/ibkr-relay && docker compose logs -f $(or $(S),poller)'

stats: ## Show container resource usage
	@. ./.env && ssh -i $${SSH_KEY:-$$HOME/.ssh/ibkr-relay} root@$$DROPLET_IP \
		'docker stats --no-stream'

gateway: ## Start IB Gateway container (then open VNC for 2FA)
	@. ./.env && ssh -i $${SSH_KEY:-$$HOME/.ssh/ibkr-relay} root@$$DROPLET_IP \
		'cd /opt/ibkr-relay && docker compose up -d ib-gateway && sleep 2 && docker compose ps ib-gateway'

ssh: ## SSH into the droplet
	@. ./.env && ssh -i $${SSH_KEY:-$$HOME/.ssh/ibkr-relay} root@$$DROPLET_IP
