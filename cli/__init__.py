"""IBKR Webhook Relay CLI — project-specific configuration.

Sets up CoreConfig and exposes IBKR-specific helpers used by
project-specific commands (order, poll, test_webhook).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from cli.core import CoreConfig, die, env, set_config

PROJECT_DIR = Path(__file__).resolve().parent.parent
PROJECT_NAME = "ibkr-relay"
REMOTE_DIR = f"/opt/{PROJECT_NAME}"


# ── IBKR-specific helpers ───────────────────────────────────────────

def validate_poller_env(suffix=""):
    required = ["IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID"]
    missing = []
    set_count = 0
    for var in required:
        full = f"{var}{suffix}"
        if os.environ.get(full):
            set_count += 1
        else:
            missing.append(full)
    if set_count == 0:
        return False
    if missing:
        die(f"Poller{suffix or ''} partially configured. Missing: {', '.join(missing)}")
    return True


def _compose_profiles():
    profiles = []
    if validate_poller_env("_2"):
        profiles.append("poller2")
    return ",".join(profiles)


def _droplet_size():
    heap = int(env("JAVA_HEAP_SIZE", "768"))
    if heap <= 1024:
        return "s-1vcpu-2gb"
    elif heap <= 3072:
        return "s-2vcpu-4gb"
    elif heap <= 6144:
        return "s-4vcpu-8gb"
    else:
        return "s-8vcpu-16gb"


def _pre_sync_hook():
    validate_poller_env("_2")
    from notifier import validate_notifier_env
    validate_notifier_env()
    validate_notifier_env("_2")


_RELAY_URLS: dict[str, str] = {
    "local": "http://localhost:15000",
}


def relay_api(path, method="POST", data=None):
    relay_env = os.environ.get("RELAY_ENV") or os.environ.get("DEFAULT_CLI_RELAY_ENV") or "prod"
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


# ── CoreConfig for IBKR project ────────────────────────────────────

_CONFIG = CoreConfig(
    project_name=PROJECT_NAME,
    project_dir=PROJECT_DIR,
    terraform_vars={
        "do_token": "DO_API_TOKEN",
        "java_heap_size": "JAVA_HEAP_SIZE",
        "vnc_domain": "VNC_DOMAIN",
        "site_domain": "SITE_DOMAIN",
    },
    required_env=[
        "DO_API_TOKEN", "TWS_USERID", "TWS_PASSWORD",
        "VNC_SERVER_PASSWORD",
        "IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID",
    ],
    service_map={
        "gateway": "ib-gateway",
        "ib-gateway": "ib-gateway",
        "novnc": "novnc",
        "vnc": "novnc",
        "caddy": "caddy",
        "relay": "webhook-relay",
        "webhook-relay": "webhook-relay",
        "poller": "poller",
        "poller2": "poller-2",
        "poller-2": "poller-2",
    },
    post_deploy_message="Open the VNC URL and complete 2FA",
    post_resume_message="Open https://{VNC_DOMAIN} to complete 2FA",
    compose_profiles_fn=_compose_profiles,
    size_selector_fn=_droplet_size,
    route_prefix="/ibkr",
    pre_sync_hook=_pre_sync_hook,
)

set_config(_CONFIG)
