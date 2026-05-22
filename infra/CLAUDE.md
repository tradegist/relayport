# `infra/` — Infrastructure backbone

Caddy reverse proxy + shared-deploy plumbing. No business logic.

For the new-routed-service checklist, see the [`add-caddy-route`](../.claude/skills/add-caddy-route/SKILL.md) skill.

## Env file flow

```
.env         ─┐
.env.relays  ─┤── env_file: in docker-compose.yml ──▶ relays container
              │
.env.droplet ─── CLI only (never pushed to container)
.env.test    ─── env_file: !override in docker-compose.test.yml ──▶ test containers
```

All secrets are injected via `env_file:` in `docker-compose.yml`. Caddy reads `SITE_DOMAIN` from its `environment:` block — the Caddyfile uses `{$SITE_DOMAIN}` syntax.

## Caddy Snippet Structure

```
infra/caddy/
  Caddyfile              # Shell: imports from sites/ and shared dirs
  sites/
    relayport.caddy      # /relays/* → relays:8000
    debug.caddy          # /debug/webhook/* → debug:9000
    market_data.caddy    # /v1/market-data/* → market_data:8001
```

The Caddyfile composes routing from snippet files via `import` directives. Each project writes one snippet. Routes must be prefixed to avoid collisions.

## Shared mode deploys

Shared projects deploy snippets to `/opt/caddy-shared/sites/` on the droplet (not into the host project's directory). The host Caddy mounts both:

- `./infra/caddy/sites/` → `/etc/caddy/sites/` (host project's own routes)
- `/opt/caddy-shared/sites/` → `/etc/caddy/shared-sites/` (shared projects' routes)

During shared deploy, snippet files are **templated** — all `{$VAR}` placeholders are replaced with literal env var values from the shared project's `.env`. This avoids requiring the host Caddy container to have the shared project's env vars.

## Critical invariant: keep `route_prefixes` in sync

**When adding a new Caddy snippet (`infra/caddy/sites/*.caddy`), always update `route_prefixes` in [cli/__init__.py](../cli/__init__.py) in the same commit.** The CLI's `_validate_site_snippet_routes` checks every `handle` directive against `route_prefixes` during shared deploy — if a new snippet's prefix isn't listed, shared deployments abort.

The full checklist for a new routed service:
1. Add the `.caddy` snippet under `infra/caddy/sites/`.
2. Add the prefix to `route_prefixes` in `cli/__init__.py`.
3. Add the token to `required_env` in `cli/__init__.py` if the service has its own auth token.
4. Add the service alias to `service_map` in `cli/__init__.py`.
