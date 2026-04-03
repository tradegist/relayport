.PHONY: setup deploy destroy pause resume sync order poll poll2 test-webhook types test typecheck logs stats gateway ssh help

help: ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  make %-12s %s\n", $$1, $$2}'

setup: ## Install dev dependencies (mypy, pydantic, pytest)
	pip3 install -r requirements-dev.txt

deploy: ## Deploy infrastructure (Terraform + Docker)
	python3 -m cli deploy

destroy: ## Permanently destroy all infrastructure
	python3 -m cli destroy

pause: ## Snapshot droplet + delete (save costs)
	python3 -m cli pause

resume: ## Restore droplet from snapshot
	python3 -m cli resume

sync: ## Push .env + restart all services (or: make sync S=gateway)
	python3 -m cli sync $(S)

order: ## Place an order (e.g. make order Q=2 SYM=TSLA T=MKT [P=] [CUR=EUR] [EX=LSE])
	python3 -m cli order $(Q) $(SYM) $(T) $(P) $(CUR) $(EX)

poll: ## Trigger an immediate Flex poll (V=1 verbose, DEBUG=1 raw XML, REPLAY=N resend)
	python3 -m cli poll $(if $(V),-v) $(if $(DEBUG),--debug) $(if $(REPLAY),--replay $(REPLAY))

poll2: ## Trigger an immediate Flex poll (second poller)
	python3 -m cli poll 2 $(if $(V),-v) $(if $(DEBUG),--debug) $(if $(REPLAY),--replay $(REPLAY))

test-webhook: ## Send sample trades to webhook endpoint (make test-webhook [S=2])
	python3 -m cli test-webhook $(S)

types: ## Regenerate TypeScript types from Pydantic models
	python3 models.py > types/webhook-payload.schema.json
	npx --yes json-schema-to-typescript types/webhook-payload.schema.json > types/webhook-payload.d.ts
	@echo "Generated types/webhook-payload.d.ts"

test: ## Run unit tests
	PYTHONPATH=. python3 -m pytest poller/ -v

typecheck: ## Run mypy strict type checking
	python3 -m mypy models.py poller/ cli/test_webhook.py

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
