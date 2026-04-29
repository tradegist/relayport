import argparse
import ipaddress
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from cli.core import (
    config,
    deploy_mode,
    die,
    env,
    load_env,
    require_env,
    scp_file,
    ssh_cmd,
    ssh_key_path,
    terraform,
)


def _deploy_standalone() -> None:
    """Deploy via Terraform (own droplet), then rsync files and start services."""
    from cli.core.sync import _run_checks, _sync_local_files

    cfg = config()

    for cmd in ["terraform", "curl", "rsync"]:
        if not shutil.which(cmd):
            die(f"'{cmd}' is required but not installed.")

    require_env(*cfg.required_env)

    _run_checks(skip_e2e=True)

    # Export TF_VAR_* for Terraform only when a source env var is present.
    # Leaving TF_VAR_* unset allows Terraform variable defaults/validation to work.
    for tf_name, env_key in cfg.terraform_vars.items():
        tf_var_key = f"TF_VAR_{tf_name}"
        env_value = os.environ.get(env_key)
        if env_value:
            os.environ[tf_var_key] = env_value
        else:
            os.environ.pop(tf_var_key, None)

    terraform("init", "-input=false")

    # If DROPLET_IP is set to a valid IP, a reserved IP from a previous
    # deployment exists on the DO account. Re-import it so Terraform reuses
    # it instead of creating a new one. Skip placeholder/invalid values.
    existing_ip = os.environ.get("DROPLET_IP", "").strip()
    try:
        if ipaddress.ip_address(existing_ip).version != 4:
            existing_ip = ""
    except ValueError:
        existing_ip = ""
    if existing_ip:
        try:
            state = terraform("state", "list", capture=True).stdout
        except subprocess.CalledProcessError:
            state = ""
        if "digitalocean_reserved_ip.relay" not in state:
            print(f"Importing existing reserved IP {existing_ip}...")
            terraform("import", "digitalocean_reserved_ip.relay", existing_ip)

    terraform("apply", "-auto-approve", "-input=false")

    droplet_ip = terraform("output", "-raw", "droplet_ip", capture=True).stdout.strip()

    # Save SSH key for subsequent sync/ssh commands
    key = terraform("output", "-raw", "ssh_private_key", capture=True).stdout
    key_path = Path(ssh_key_path())
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600

    # Rsync project files to the droplet (skip host key check — new droplet)
    _sync_local_files(droplet_ip, strict_host_check=False)

    # Push env files to droplet
    print("Pushing env files to droplet...")
    scp_file(cfg.project_dir / ".env", f"{cfg.remote_dir}/.env", droplet_ip)
    relays_env = cfg.project_dir / ".env.relays"
    if relays_env.exists():
        scp_file(relays_env, f"{cfg.remote_dir}/.env.relays", droplet_ip)

    # Start the stack
    profiles = cfg.compose_profiles()
    compose_env = cfg.compose_env()
    print("Starting services...")
    ssh_cmd(droplet_ip,
            f"cd {cfg.remote_dir} && {compose_env}COMPOSE_PROFILES='{profiles}' "
            f"docker compose up -d --build")

    print()
    print("=" * 44)
    print("  Deployment complete!")
    print("=" * 44)
    print()
    print(f"  Droplet IP:  {droplet_ip}")
    print(f"  SSH key:     {key_path}")
    print()
    print("  Next steps:")
    print(f"  1. Add DROPLET_IP={droplet_ip} to .env.droplet")
    if cfg.post_deploy_message:
        print(f"  2. {cfg.post_deploy_message}")
    print()


def _template_caddy_snippet(src: Path) -> str:
    """Pre-template a Caddy snippet by substituting {$VAR} and {$VAR:-default} placeholders.

    This is the CLI's own pre-templating step, run before snippets are uploaded to Caddy.
    Supports both bash-style ``{$VAR:-default}`` and Caddy-native ``{$VAR:default}``
    (single colon) — both are substituted here so Caddy never needs to expand them at
    runtime (the Caddy container does not have access to the CLI's env vars).

    Substitutes each ``{$NAME}`` with the corresponding environment variable.
    ``{$NAME:-default}`` / ``{$NAME:default}`` uses the default when the env var is unset or empty.
    Raises if any required var (no default) is not set.
    """
    content = src.read_text()
    pattern = re.compile(r'\{\$([A-Z_][A-Z0-9_]*)(?::-?([^}]*))?\}')
    refs = pattern.findall(content)
    if not refs:
        return content
    missing = [m.group(1) for m in pattern.finditer(content) if m.group(2) is None and not os.environ.get(m.group(1))]
    if missing:
        die(f"Caddy snippet {src.name} references undefined env vars: "
            f"{', '.join(missing)}\nSet them in .env before deploying.")

    def _sub(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2) or ""
        return os.environ.get(name) or default

    return pattern.sub(_sub, content)


def _validate_site_snippet_routes(content: str, snippet_name: str, prefixes: list[str]) -> None:
    """Ensure all ``handle`` paths in a site snippet start with one of *prefixes*.

    Prevents a misconfigured snippet from shadowing other projects'
    routes on the shared Caddy instance.
    """
    for match in re.finditer(r'^\s*handle\s+(\S+)', content, re.MULTILINE):
        path = match.group(1)
        if not any(path.startswith(f"{p}/") for p in prefixes):
            allowed = ", ".join(f"'{p}/*'" for p in prefixes)
            die(f"Snippet {snippet_name}: handle path '{path}' does not start "
                f"with any project prefix ({allowed}). All site snippet routes "
                f"must be namespaced under a project prefix to avoid collisions.")


def _deploy_caddy_snippets(droplet_ip: str) -> None:
    """Copy project Caddy snippets to /opt/caddy-shared/ and reload Caddy."""
    cfg = config()
    caddy_dir = cfg.project_dir / "infra" / "caddy"
    deployed = False

    ssh_cmd(droplet_ip,
            "mkdir -p /opt/caddy-shared/sites /opt/caddy-shared/domains")

    for subdir in ("sites", "domains"):
        src_dir = caddy_dir / subdir
        if not src_dir.is_dir():
            continue
        for snippet in src_dir.glob("*.caddy"):
            templated = _template_caddy_snippet(snippet)
            if subdir == "sites" and cfg.route_prefixes:
                _validate_site_snippet_routes(
                    templated, snippet.name, cfg.route_prefixes)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".caddy", delete=False,
            ) as tmp:
                tmp.write(templated)
                tmp_path = tmp.name
            try:
                scp_file(tmp_path, f"/opt/caddy-shared/{subdir}/{snippet.name}", droplet_ip)
            finally:
                os.unlink(tmp_path)
            deployed = True
            print(f"  Deployed snippet: {subdir}/{snippet.name}")

    if deployed:
        print("Reloading Caddy configuration...")
        ssh_cmd(droplet_ip,
                "docker exec "
                "$(docker ps --filter label=com.docker.compose.service=caddy --format '{{.Names}}' | head -1) "
                "caddy reload --config /etc/caddy/Caddyfile")


def _deploy_shared() -> None:
    """Deploy to an existing shared droplet (no Terraform)."""
    from cli.core.sync import _run_checks, _sync_local_files

    cfg = config()
    droplet_ip = env("DROPLET_IP")
    profiles = cfg.compose_profiles()

    _run_checks(skip_e2e=True)
    _sync_local_files(droplet_ip)

    print("Pushing env files to droplet...")
    scp_file(cfg.project_dir / ".env", f"{cfg.remote_dir}/.env", droplet_ip)
    relays_env = cfg.project_dir / ".env.relays"
    if relays_env.exists():
        scp_file(relays_env, f"{cfg.remote_dir}/.env.relays", droplet_ip)

    compose_env = cfg.compose_env()
    print("Starting services (shared mode)...")
    ssh_cmd(droplet_ip,
            f"cd {cfg.remote_dir} && {compose_env}COMPOSE_PROFILES='{profiles}' "
            f"docker compose -f docker-compose.yml -f docker-compose.shared.yml "
            f"up -d --build --force-recreate")

    _deploy_caddy_snippets(droplet_ip)

    print()
    print("=" * 44)
    print("  Shared deployment complete!")
    print("=" * 44)
    print()


def run(args: argparse.Namespace) -> None:
    load_env()

    mode = deploy_mode()

    if mode == "standalone":
        _deploy_standalone()
    else:
        _deploy_shared()
