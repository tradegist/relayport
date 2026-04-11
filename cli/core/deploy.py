import os
import re
import shutil
import stat
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


def _deploy_standalone():
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

    # Push .env with secrets
    print("Pushing .env to droplet...")
    scp_file(cfg.project_dir / ".env", f"{cfg.remote_dir}/.env", droplet_ip)

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
    print(f"  1. Add DROPLET_IP={droplet_ip} to .env")
    if cfg.post_deploy_message:
        print(f"  2. {cfg.post_deploy_message}")
    print()


def _template_caddy_snippet(src: Path) -> str:
    """Replace Caddy {$VAR} placeholders with env var values.

    Finds all ``{$NAME}`` patterns in the file and substitutes them
    with the corresponding environment variable. Raises if any
    referenced env var is not set.
    """
    content = src.read_text()
    refs = re.findall(r'\{\$([A-Z_][A-Z0-9_]*)\}', content)
    if not refs:
        return content
    missing = [name for name in refs if not os.environ.get(name)]
    if missing:
        die(f"Caddy snippet {src.name} references undefined env vars: "
            f"{', '.join(missing)}\nSet them in .env before deploying.")
    for name in refs:
        content = content.replace(f"{{${name}}}", os.environ[name])
    return content


def _validate_site_snippet_routes(content: str, snippet_name: str, prefix: str) -> None:
    """Ensure all ``handle`` paths in a site snippet start with *prefix*.

    Prevents a misconfigured snippet from shadowing other projects'
    routes on the shared Caddy instance.
    """
    for match in re.finditer(r'^\s*handle\s+(\S+)', content, re.MULTILINE):
        path = match.group(1)
        if not path.startswith(f"{prefix}/"):
            die(f"Snippet {snippet_name}: handle path '{path}' does not start "
                f"with project prefix '{prefix}/'. All site snippet routes "
                f"must be namespaced under '{prefix}/*' to avoid collisions.")


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
            if subdir == "sites" and cfg.route_prefix:
                _validate_site_snippet_routes(
                    templated, snippet.name, cfg.route_prefix)
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
                "docker exec caddy caddy reload --config /etc/caddy/Caddyfile")


def _deploy_shared():
    """Deploy to an existing shared droplet (no Terraform)."""
    from cli.core.sync import _run_checks, _sync_local_files

    cfg = config()
    droplet_ip = env("DROPLET_IP")
    profiles = cfg.compose_profiles()

    _run_checks(skip_e2e=True)
    _sync_local_files(droplet_ip)

    print("Pushing .env to droplet...")
    scp_file(cfg.project_dir / ".env", f"{cfg.remote_dir}/.env", droplet_ip)

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


def run(args):
    load_env()

    mode = deploy_mode()

    if mode == "standalone":
        _deploy_standalone()
    else:
        _deploy_shared()
