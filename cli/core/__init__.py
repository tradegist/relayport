"""Project-agnostic CLI helpers for DO droplet + Docker Compose projects.

Every helper here is generic — no project-specific logic.
Project-specific config is injected via ``CoreConfig``.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast, overload

_UNSET = object()
_VALID_DEPLOY_MODES = ("standalone", "shared")


# ── CoreConfig ──────────────────────────────────────────────────────

@dataclass
class CoreConfig:
    """Project-specific configuration consumed by all core CLI commands."""

    project_name: str
    """Used for droplet/snapshot naming, SSH key default, etc."""

    project_dir: Path
    """Root of the project repository (absolute path)."""

    terraform_vars: dict[str, str]
    """Mapping of TF_VAR_name → env-var-key. Values are resolved via ``env()``
    at call time. Use ``env(key, "")`` for optional vars."""

    required_env: list[str]
    """Env vars that must be set for standalone deploy."""

    service_map: dict[str, str]
    """Alias → Docker Compose service name mapping for ``sync`` command."""

    post_deploy_message: str = ""
    """Printed after standalone deploy completes (e.g. '2. Open VNC for 2FA')."""

    post_resume_message: str = ""
    """Printed after resume completes (e.g. 'Open VNC to complete 2FA')."""

    compose_profiles_fn: Callable[[], str] | None = None
    """Optional callback returning COMPOSE_PROFILES value (e.g. 'poller2')."""

    compose_env_fn: Callable[[], dict[str, str]] | None = None
    """Optional callback returning extra env vars to prepend to docker compose
    commands (e.g. ``{'POLLER_REPLICAS': '0'}``). Shell env vars override
    anything in ``.env``, so this is used for derived values."""

    size_selector_fn: Callable[[], str] | None = None
    """Optional callback returning droplet size slug for resume.
    If None, defaults to 's-1vcpu-1gb'."""

    route_prefixes: list[str] = []
    """Expected path prefixes for Caddy site snippets (e.g. ['/ibkr']).
    If set, deploy validates that all ``handle`` directives in
    ``infra/caddy/sites/*.caddy`` start with one of these prefixes."""

    pre_sync_hook: Callable[[], None] | None = None
    """Optional callback run before sync (e.g. validate poller-2 env)."""

    @property
    def remote_dir(self) -> str:
        return f"/opt/{self.project_name}"

    def compose_profiles(self) -> str:
        if self.compose_profiles_fn:
            return self.compose_profiles_fn()
        return ""

    def compose_env(self) -> str:
        """Return shell env var assignments to prepend to docker compose commands."""
        if self.compose_env_fn:
            env_dict = self.compose_env_fn()
            if env_dict:
                return " ".join(f"{k}='{v}'" for k, v in env_dict.items()) + " "
        return ""

    def droplet_size(self) -> str:
        if self.size_selector_fn:
            return self.size_selector_fn()
        return "s-1vcpu-1gb"


# ── Singleton config ────────────────────────────────────────────────

_config: CoreConfig | None = None


def set_config(cfg: CoreConfig) -> None:
    global _config
    _config = cfg


def config() -> CoreConfig:
    if _config is None:
        raise RuntimeError("CoreConfig not set — call set_config() from cli/__init__.py")
    return _config


# ── Core subparser registration ─────────────────────────────────────

CORE_MODULES: dict[str, str] = {
    "deploy": "cli.core.deploy",
    "destroy": "cli.core.destroy",
    "pause": "cli.core.pause",
    "resume": "cli.core.resume",
    "sync": "cli.core.sync",
}


def register_parsers(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Register core subcommands (deploy, destroy, pause, resume, sync)."""
    sub.add_parser("deploy", help="Deploy infrastructure (Terraform + Docker)")
    sub.add_parser("destroy", help="Permanently destroy all infrastructure")
    sub.add_parser("pause", help="Snapshot droplet + delete (save costs)")
    sub.add_parser("resume", help="Restore droplet from snapshot")

    p = sub.add_parser("sync", help="Push .env + restart services")
    p.add_argument("services", nargs="*", help="Services to restart (default: all)")
    p.add_argument("--local-files", action="store_true",
                   help="Rsync files to droplet before restart (implies --build)")
    p.add_argument("--build", action="store_true",
                   help="Rebuild Docker images before restarting")
    p.add_argument("--skip-e2e", action="store_true",
                   help="Skip E2E tests during --local-files pre-deploy checks")


# ── Generic helpers ─────────────────────────────────────────────────

def die(msg: str) -> "None":
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _load_env_file(path: Path) -> None:
    """Load a single .env file into os.environ."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value


def load_env(path: str | Path | None = None) -> None:
    """Load project env files into os.environ.

    When *path* is given, loads that single file.
    Otherwise loads .env.droplet (optional), .env (required),
    and .env.relays (optional) from the project root.
    """
    if path:
        p = Path(path)
        if not p.exists():
            die(f"Env file not found: {p}")
        _load_env_file(p)
        return

    cfg = _config
    project_dir = cfg.project_dir if cfg else Path(".")

    env_path = project_dir / ".env"
    if not env_path.exists():
        die(".env not found. Run 'make setup' or copy env_examples/env to .env")

    for name in (".env.droplet", ".env", ".env.relays"):
        f = project_dir / name
        if f.exists():
            _load_env_file(f)


@overload
def env(key: str) -> str: ...
@overload
def env(key: str, default: str) -> str: ...
def env(key: str, default: str | object = _UNSET) -> str:
    val = os.environ.get(key)
    if val is None:
        if default is _UNSET:
            die(f"{key} is not set in .env or .env.droplet")
        return cast(str, default)
    return val


def require_env(*keys: str) -> None:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        die(f"Missing required vars (not set in .env or .env.droplet): {', '.join(missing)}")


def deploy_mode() -> str:
    mode = os.environ.get("DEPLOY_MODE", "").lower()
    if mode not in _VALID_DEPLOY_MODES:
        die(f"DEPLOY_MODE must be set to 'standalone' or 'shared' in .env or .env.droplet (got: {mode!r})")
    return mode


def is_shared() -> bool:
    return deploy_mode() == "shared"


# ── SSH ─────────────────────────────────────────────────────────────

def ssh_key_path() -> str:
    return os.environ.get("SSH_KEY", str(Path.home() / ".ssh" / config().project_name))


def ssh_cmd(
    ip: str,
    command: str,
    strict_host_check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = ["ssh", "-i", ssh_key_path()]
    if not strict_host_check:
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5"]
    cmd += [f"root@{ip}", command]
    if capture:
        return subprocess.run(cmd, check=True, capture_output=True, text=True)
    return subprocess.run(cmd, check=True, text=True)


def scp_file(
    local_path: str | Path,
    remote_path: str,
    ip: str,
    strict_host_check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["scp", "-i", ssh_key_path()]
    if not strict_host_check:
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5"]
    cmd += [str(local_path), f"root@{ip}:{remote_path}"]
    return subprocess.run(cmd, check=True, text=True)


# ── DigitalOcean API ────────────────────────────────────────────────

def do_api(method: str, path: str, data: dict[str, object] | None = None) -> dict[str, object] | None:
    token = env("DO_API_TOKEN")
    url = f"https://api.digitalocean.com/v2{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
            return json.loads(content) if content else None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        die(f"DO API error ({e.code} {method} {path}): {err_body}")
        return None  # unreachable, satisfies type checker


# ── Terraform ───────────────────────────────────────────────────────

def terraform(
    *args: str,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = ["terraform", *args]
    cwd = str(config().project_dir / "terraform")
    if capture:
        return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
    return subprocess.run(cmd, cwd=cwd, check=True, text=True)
