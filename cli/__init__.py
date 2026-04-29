"""RelayPort CLI — project-specific configuration.

Sets up CoreConfig and exposes project-specific helpers used by
project-specific commands (poll, test_webhook).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cli.core import CoreConfig, die, env, set_config
from relay_core.notifier import validate_notifier_env

PROJECT_DIR = Path(__file__).resolve().parent.parent
PROJECT_NAME = "relayport"
REMOTE_DIR = f"/opt/{PROJECT_NAME}"


# ── Project-specific helpers ─────────────────────────────────────────


def _compose_env():
    """Compute derived env vars for docker compose commands."""
    env_vars: dict[str, str] = {}

    # DEBUG_REPLICAS: auto-enable debug service when DEBUG_WEBHOOK_PATH is set
    if os.environ.get("DEBUG_WEBHOOK_PATH", "").strip():
        env_vars["DEBUG_REPLICAS"] = "1"

    return env_vars


def _droplet_size():
    override = os.environ.get("DROPLET_SIZE", "")
    if override:
        return override
    # Poller-only needs minimal resources
    return "s-1vcpu-512mb"


def _pre_sync_hook():
    # Validate notifiers for each configured relay (prefix fallback to generic)
    relays_raw = os.environ.get("RELAYS", "").strip()
    if relays_raw:
        for name in relays_raw.split(","):
            name = name.strip().lower()
            if name:
                validate_notifier_env(prefix=f"{name.upper()}_")
    else:
        validate_notifier_env()


_RELAY_URLS: dict[str, str] = {
    "local": "http://localhost:15001",
}


def get_relay_env() -> str:
    """Return 'local' or 'prod' based on RELAY_ENV / DEFAULT_CLI_ENV."""
    return (
        os.environ.get("RELAY_ENV")
        or os.environ.get("DEFAULT_CLI_ENV")
        or "prod"
    )


def relay_api(path: str, method: str = "POST", data: dict[str, object] | None = None) -> Any:
    relay_env = get_relay_env()
    base_url = _RELAY_URLS.get(relay_env)
    if base_url:
        url = f"{base_url}{path}"
    else:
        domain = env("SITE_DOMAIN")
        url = f"https://{domain}{path}"
    token = env("API_TOKEN")
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        content = e.read().decode()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            die(f"Request failed ({e.code}): {content}")


# ── CoreConfig for RelayPort project ──────────────────────────────

_CONFIG = CoreConfig(
    project_name=PROJECT_NAME,
    project_dir=PROJECT_DIR,
    terraform_vars={
        "do_token": "DO_API_TOKEN",
        "droplet_size": "DROPLET_SIZE",
        "site_domain": "SITE_DOMAIN",
    },
    required_env=[
        "DO_API_TOKEN",
        "API_TOKEN",
    ],
    service_map={
        "caddy": "caddy",
        "relays": "relays",
        "debug": "debug",
    },
    compose_env_fn=_compose_env,
    size_selector_fn=_droplet_size,
    route_prefixes=["/relays", "/debug"],
    pre_sync_hook=_pre_sync_hook,
)

set_config(_CONFIG)

