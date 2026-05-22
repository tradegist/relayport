---
name: add-caddy-route
description: Checklist to add a new routed service to the Caddy reverse proxy. Use when the user asks to "expose service X through Caddy", "add a new endpoint at /foo", "route /bar to a container", or otherwise adds a new public route. Covers the four-step contract (snippet + route_prefixes + token + service_map).
---

# Adding a new routed service through Caddy

Adding a new service that should be reachable via the public `SITE_DOMAIN` requires updating four places in lockstep. **All four must be in the same commit** — partial changes break shared deploys.

## The four-step checklist

1. **Add the `.caddy` snippet** under `infra/caddy/sites/<name>.caddy`. Use `relayport.caddy`, `debug.caddy`, or `market_data.caddy` as a template. Each snippet defines `handle /<prefix>/*` blocks inside the implicit `{$SITE_DOMAIN}` site.

2. **Add the prefix to `route_prefixes`** in `cli/__init__.py`. The CLI's `_validate_site_snippet_routes` checks every `handle` directive against this list during shared deploy — if a new snippet's prefix isn't listed, shared deployments abort.

3. **Add the token to `required_env`** in `cli/__init__.py` if the service has its own auth token (like `MD_API_TOKEN`). The CLI's pre-deploy validation refuses to push when required tokens are missing or empty.

4. **Add the service alias to `service_map`** in `cli/__init__.py`. This is the human-friendly name (`market-data`) → container name (`market_data`) mapping used by `make logs S=<alias>`.

## Example: adding a `/v1/market-data/*` route

```caddy
# infra/caddy/sites/market_data.caddy
handle /v1/market-data/* {
    reverse_proxy market_data:8001
}
```

```python
# cli/__init__.py
route_prefixes = [
    "/relays/",
    "/debug/webhook/",
    "/v1/market-data/",  # NEW
]

required_env = [
    "API_TOKEN",
    "MD_API_TOKEN",  # NEW
    ...
]

service_map = {
    "relays": "relays",
    "debug": "debug",
    "market-data": "market_data",  # NEW
}
```

## Shared-mode considerations

For shared deploys (`DEPLOY_MODE=shared`), the snippet is copied to `/opt/caddy-shared/sites/` on the droplet and **templated** — all `{$VAR}` placeholders are replaced with literal env var values from the shared project's `.env` before Caddy reads it. This avoids requiring the host Caddy container to have the shared project's env vars.

If your snippet needs an env var that the host Caddy doesn't have, write it as `{$YOUR_VAR}` in the snippet — it will be substituted at deploy time. The host Caddy never needs to know about it.

## Verifying

- `python3 -m cli sync` should print "Validating site snippet routes against route_prefixes..." and continue without errors.
- After deploy, `curl https://$SITE_DOMAIN/<your-prefix>/health` should reach your service.
- For shared mode, also verify the snippet was templated: `ssh root@$DROPLET_IP "grep -E 'VAR_NAME' /opt/caddy-shared/sites/<name>.caddy"` should show literal values, not `{$VAR_NAME}` placeholders.

## Why this exists

Past incidents:
- A new service was added without `route_prefixes` update → shared deploy bombed with a confusing "unknown route" error.
- A new auth-protected service was deployed with `API_TOKEN=""` → auth was silently disabled (the empty-token check in the auth middleware caught it, but the deploy still went through). `required_env` validation now blocks deploys when tokens are missing or empty.
