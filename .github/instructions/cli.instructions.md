---
applyTo: "cli/**,Makefile,docker-compose*.yml,env_examples/**,terraform/**"
---

# `cli/` — Operator CLI

Stdlib-only Python CLI for deploying and operating the relay stack. Invoked via `python3 -m cli <command>` or `make`.

## Makefile must mirror CLI arguments

- When adding a new parameter to a `cli/` command, always add the corresponding `$(if $(VAR),--flag $(VAR))` to the Makefile target so `make <target> VAR=value` works.
- **CLI parameters that are optional in the Makefile must be named flags (`--currency`, `--exchange`), never positional args.** When the Makefile uses `$(if $(VAR),...)`, omitting `VAR` omits the entire argument — if the CLI parameter is positional, downstream args shift into the wrong position and get silently misparsed.

## Environment Files

Configuration is split into four env files to separate concerns:

- **`.env`** — App-level config: `SITE_DOMAIN`, `API_TOKEN`, `NOTIFIERS`, `RELAYS`, `POLL_INTERVAL`, listener settings. Injected into the `relays` container via `env_file:` in `docker-compose.yml`. Pushed to the droplet by `make sync` / `make deploy`.
- **`.env.relays`** — Relay-prefixed env vars: `IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID`, relay-specific overrides (`IBKR_NOTIFIERS`, `IBKR_TARGET_WEBHOOK_URL`). Injected via `env_file:` (`required: false`).
- **`.env.droplet`** — Developer-machine-only vars (never pushed): `DEPLOY_MODE`, `DO_API_TOKEN`, `DROPLET_IP`, `SSH_KEY`, `DROPLET_SIZE`, `DEFAULT_CLI_ENV`. Only read by `cli/` and the Makefile.
- **`.env.test`** — E2E test config. Used only in `docker-compose.test.yml` via `env_file: !override`.

Templates live in `env_examples/`. `make setup` copies them to `.<name>` if missing.

## Local Development

- **`.venv`** is the project's virtual environment. Created by `make setup` using Homebrew Python.
- **Auto-activation** via a `chpwd` hook in `~/.zshrc`.
- **`make setup`** creates `.venv` (if missing), installs all deps (`requirements-dev.txt` + `services/relay_core/requirements.txt`), writes a `.pth` file (`relayport.pth` adds `services/debug/`, `services/`, `services/relay_core/` to `sys.path`), and copies `env_examples/*` → `.<name>`. Auto-heals a broken `.venv` after a Python upgrade by detecting a missing `pip` import and rebuilding.
- **`.venv/` is gitignored.**
- **`docker-compose.local.yml`** adds bind mounts that shadow the `COPY`'d files in the image with your local source tree (`:ro`). Code changes are visible on container restart — no rebuild needed.
- **`make sync` respects `DEFAULT_CLI_ENV`.** `local` → restart local compose stack. `prod` (default) → full CLI sync to the droplet. Override per-command with `ENV=local` or `ENV=prod`.
- **`make logs` also respects `DEFAULT_CLI_ENV`.** `make logs S=debug` streams local container logs when local, droplet logs when prod.

## Deployment Modes

Controlled by `DEPLOY_MODE` in `.env.droplet` (required, validated before any deploy or sync).

### Standalone Mode (`DEPLOY_MODE=standalone`)

- Set `DO_API_TOKEN` in `.env.droplet`. `make deploy` runs Terraform to create a droplet + firewall + reserved IP, then the CLI rsyncs project files, pushes `.env` + `.env.relays`, and runs `docker compose up -d --build`.
- Terraform only creates infrastructure — cloud-init installs Docker and creates the project directory. The CLI handles all file transfer and service startup.
- After deploy, add `DROPLET_IP` from terraform output to `.env.droplet` for `make sync`.
- `DO_API_TOKEN` can be removed after first deploy. Mode is determined by `DEPLOY_MODE`, not by token presence.

### Shared Mode (`DEPLOY_MODE=shared`)

- Set `DROPLET_IP` and `SSH_KEY` in `.env.droplet` (no `DO_API_TOKEN` needed). Set `SHARED_NETWORK` in `.env` — **required**; CLI fails fast with a clear error if unset.
- `make deploy` rsyncs files, pushes `.env` + `.env.relays`, ensures the shared Docker network exists on the droplet, and starts services with the `docker-compose.shared.yml` + `docker-compose.shared-network.yml` overlays.
- `docker-compose.shared.yml` disables Caddy (the host project runs it). `docker-compose.shared-network.yml` marks the shared network as `external: true`.
- Caddy snippet files must be deployed to the host project's Caddy to enable routing.
- `make sync` uses both overlays automatically.

### Shared Network (`SHARED_NETWORK`)

- Base `docker-compose.yml` uses `name: ${SHARED_NETWORK:-}` on the default network. Unset → isolated project-scoped network. Set → CLI **always** applies `docker-compose.shared-network.yml`, which adds `external: true` on top.
- `SHARED_NETWORK` may live in either `.env` or `.env.droplet`. The CLI loads both and explicitly injects `SHARED_NETWORK='<value>'` into the remote `docker compose` command env via `shared_network_compose_env()`. Shell-env precedence beats the droplet's `.env`. `.env` is recommended since it's the only file scp'd to the droplet (manual `docker compose up` will also find it).
- **The CLI is the network owner.** Before `docker compose up`, `cli/core/__init__.py::ensure_shared_network` runs `docker network inspect <name> >/dev/null 2>&1 || docker network create <name>` on the droplet. Idempotent. Removes any ordering dependency between projects.
- **Running `docker compose up` manually on the droplet bypasses the CLI's overlay assembly** and re-introduces the "network was not created for project X" warning. Always go through `make sync` / `make deploy` for production operations.

## Droplet Sizing

- **`DROPLET_SIZE`** sets the DigitalOcean droplet slug directly (e.g. `s-1vcpu-512mb`).
- `cli/__init__.py::_droplet_size()` reads `DROPLET_SIZE`. `cli/core/resume.py` uses `cfg.droplet_size()` which delegates to the same getter.

## Deployment Model (MANDATORY)

- **`make sync LOCAL_FILES=1` uses rsync** to transfer files from the local working tree to `/opt/relayport/` on the droplet. Does NOT use git on the droplet — no clone, no deploy keys, no GitHub access needed from the server.
- **Guards:** Must be on `main` branch with a clean working tree. Ensures rsync deploys a known committed state.
- **`--delete` flag:** rsync removes files on the droplet that no longer exist locally. Correct for renames/deletions, dangerous for server-generated files.
- **Invariant: the project directory (`/opt/relayport/`) contains only source files.** No service or container may write into the project directory. All runtime-generated data (databases, caches, logs, certificates) MUST use Docker named volumes (`dedup-data:/data/dedup`, `relay-meta:/data/meta`, `caddy-data:/data`). Docker volumes live under `/var/lib/docker/volumes/`, safe from rsync `--delete`.
- **When adding new runtime data**, create a Docker named volume in `docker-compose.yml` and mount it. Never write to a path inside `/opt/relayport/`.
- **`.deployed-sha`** is the only server-side file inside the project directory. Written by `cli/sync.py` after each `--local-files` sync; excluded from rsync `--delete`. Records the deployed commit SHA.
- **rsync exclusions:** `.git/`, `.env`, `.env.relays`, `.env.droplet`, `.env.test`, `.deployed-sha`, and everything in `.gitignore` (via `--filter ':- .gitignore'`).

## Post-Deploy Sanity Check

- After every `make sync LOCAL_FILES=1` and `make deploy`, the CLI runs a best-effort sanity check: SSHes into the droplet from Python (using the existing `ssh_cmd` helper), captures `docker compose ps` + recent logs (`--since 5m --tail 100` — note `--tail` is **per service**, so the total is capped to 50 KB before being sent to claude), then pipes the captured text via stdin to `claude --print --model claude-sonnet-4-6` for summarization. Claude runs with **no tools** — pure text-in / text-out, so there's no permission bypass and no risk of agent-driven shell execution.
- **Best-effort only.** Failures never abort the deploy. Skipped gracefully when: `claude` CLI not on PATH, SSH fails, network/auth/rate-limit errors (non-zero exit), or the claude call hangs (60s timeout).
- **Opt-out:** `SKIP_POST_DEPLOY_CHECK=1` env var, `--skip-post-check` CLI flag, or `make sync SKIP_POST_CHECK=1` / `make deploy SKIP_POST_CHECK=1`.
- Plain `make sync` (without `LOCAL_FILES=1`) does not trigger the check — only file-syncing deploys do.
- **Run on demand:** `make sanity-check-deployment` (or `python3 -m cli sanity-check-deployment`) runs the same check without a sync/deploy. Useful for ad-hoc "is the droplet OK right now?" probes. **Ignores `SKIP_POST_DEPLOY_CHECK` and `--skip-post-check`** — the operator explicitly invoked the command, so both opt-outs are bypassed; only `claude not on PATH` will skip.

## Build & Deploy commands

```bash
make deploy    # Standalone: Terraform | Shared: rsync + compose (reads .env.droplet)
make sync      # Push .env + .env.relays to droplet + restart services
make sync LOCAL_FILES=1  # rsync files + rebuild + restart + claude sanity check
make sync SKIP_POST_CHECK=1  # skip the post-deploy claude sanity check
make destroy   # Terraform destroy
make pause     # Snapshot + delete droplet (save costs)
make resume    # Restore from snapshot
make sanity-check-deployment  # Run the claude sanity check against the droplet
make poll      # Trigger immediate poll (RELAY=ibkr, IDX=1)
make watermark-reset    # Reset timestamp watermark [RELAY=ibkr or empty] [ENV=local]
make ibkr-flex-dump     # Dump live IBKR Flex XML to services/relays/ibkr/fixtures/raw.xml
make ibkr-flex-refresh  # Fetch live Flex, sanitize, write fixture
make e2e       # Run E2E tests (starts/stops stack)
make lint      # Run ruff linter (FIX=1 to auto-fix)
```

Direct CLI:
```bash
python3 -m cli deploy
python3 -m cli sync --local-files
python3 -m cli poll ibkr 1
python3 -m cli watermark-reset --relay ibkr
```

## Docker

- **`env_file:` is the correct pattern for the `relays` service.** Base `docker-compose.yml` declares `env_file: [.env, path: .env.relays, required: false]`. Injects all app-level + relay-specific vars without enumerating each one. Only guards (`API_TOKEN: ${API_TOKEN:?...}`) and vars with compose-level defaults (`POLL_INTERVAL: ${POLL_INTERVAL:-600}`) appear in `environment:`.
- **Test isolation uses `env_file: !override`.** `docker-compose.test.yml` overrides with `env_file: !override` followed by `- .env.test`, replacing rather than appending.
- **`DEBUG_WEBHOOK_PATH`** enables the `debug` container. When set in `.env`, the compose default `${DEBUG_REPLICAS:-0}` is overridden to `1` by the CLI's `_compose_env()`.
- **`.dockerignore` uses an allowlist** (`*` then `!services/<module>/**` for each module). New source files within an existing module require **no** `.dockerignore` changes.
- **When adding a new standalone module** (`services/foo/`), add `!services/foo/**` plus test/pycache exclusions to `.dockerignore`, and add the corresponding `COPY` to the Dockerfile. Without this the build context excludes the module.
