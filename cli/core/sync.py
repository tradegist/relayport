import argparse
import os
import shutil
import subprocess

from cli.core import (
    CoreConfig,
    config,
    die,
    ensure_shared_network,
    env,
    is_shared,
    load_env,
    scp_file,
    shared_network,
    shared_network_compose_env,
    shared_network_compose_flag,
    ssh_cmd,
    ssh_key_path,
)
from cli.core.sanity_check import post_deploy_sanity_check


def _test_subprocess_env(cfg: CoreConfig) -> dict[str, str]:
    """Build the env for pre-deploy test subprocesses.

    ``load_env()`` populates ``os.environ`` from ``.env`` so the CLI can do
    its own work, but those values include real production credentials
    (Resend keys, webhook URLs, etc.). Test subprocesses inherit ``os.environ``
    by default, so a test bug that reaches the real code path can emit real
    external IO. Strip the names listed in ``cfg.test_env_strip`` to make that
    impossible regardless of test author mistakes.
    """
    return {k: v for k, v in os.environ.items() if k not in cfg.test_env_strip}


def _run_checks(skip_e2e: bool) -> None:
    """Run pre-deploy checks: branch, clean tree, typecheck, tests, E2E."""
    cfg = config()

    # Must be on main branch
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True, cwd=cfg.project_dir,
    ).stdout.strip()
    if branch != "main":
        die(f"Cannot sync: on branch '{branch}', switch to 'main' first")

    # Working tree must be clean
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, check=True, cwd=cfg.project_dir,
    ).stdout.strip()
    if dirty:
        die("Cannot sync: uncommitted changes — commit or stash first")

    test_env = _test_subprocess_env(cfg)

    print("Running type checks...")
    subprocess.run(["make", "typecheck"], check=True, cwd=cfg.project_dir, env=test_env)

    print("Running linter...")
    subprocess.run(["make", "lint"], check=True, cwd=cfg.project_dir, env=test_env)

    print("Running unit tests...")
    subprocess.run(["make", "test"], check=True, cwd=cfg.project_dir, env=test_env)

    if skip_e2e:
        print("Skipping E2E tests (--skip-e2e)")
    else:
        print("Running E2E tests...")
        subprocess.run(["make", "e2e"], check=True, cwd=cfg.project_dir, env=test_env)


def _sync_local_files(droplet_ip: str, *, strict_host_check: bool = True) -> None:
    """Rsync project files to the droplet."""
    cfg = config()

    if not shutil.which("rsync"):
        die("rsync is required for --local-files "
            "(install via: brew install rsync / apt install rsync)")

    host_check = "yes" if strict_host_check else "no"
    print("Syncing files to droplet...")
    cmd = [
        "rsync", "-az", "--delete",
        "-e", f"ssh -i {ssh_key_path()} -o StrictHostKeyChecking={host_check}",
        "--filter", ":- .gitignore",
        "--exclude", ".git/",
        "--exclude", ".env",
        "--exclude", ".env.droplet",
        "--exclude", ".env.relays",
        "--exclude", ".env.test",
        "--exclude", ".deployed-sha",
        f"{cfg.project_dir}/",
        f"root@{droplet_ip}:{cfg.remote_dir}/",
    ]
    subprocess.run(cmd, check=True)

    # Write deployed commit SHA for traceability
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, cwd=cfg.project_dir,
    ).stdout.strip()
    ssh_cmd(droplet_ip, f"echo '{sha}' > {cfg.remote_dir}/.deployed-sha")
    print(f"Deployed commit: {sha[:12]}")


def run(args: argparse.Namespace) -> None:
    load_env()
    cfg = config()

    droplet_ip = env("DROPLET_IP")
    profiles = cfg.compose_profiles()

    if cfg.pre_sync_hook:
        cfg.pre_sync_hook()

    if args.local_files:
        _run_checks(args.skip_e2e)
        _sync_local_files(droplet_ip)
    elif shared_network_compose_flag():
        # `make sync` (without --local-files) doesn't rsync project files, so
        # the shared-network overlay must be pushed explicitly. With
        # --local-files the rsync above has already pushed the same file.
        scp_file(
            cfg.project_dir / "docker-compose.shared-network.yml",
            f"{cfg.remote_dir}/docker-compose.shared-network.yml",
            droplet_ip,
        )

    build = "--build " if (args.build or args.local_files) else ""

    # Assemble compose overlays: shared-mode (disable Caddy) and/or shared-network
    # (mark relay-net as external). Either may apply independently — e.g. a
    # standalone host project still uses the shared-network overlay when it sets
    # SHARED_NETWORK so it joins the same externally-managed network.
    if is_shared() and not shared_network():
        die("SHARED_NETWORK must be set (in .env or .env.droplet) when "
            "DEPLOY_MODE=shared — it names the Docker network shared with "
            "the host project.")
    overlays = ""
    if is_shared():
        overlays += "-f docker-compose.shared.yml "
    overlays += shared_network_compose_flag()
    compose_files = f"-f docker-compose.yml {overlays}" if overlays else ""

    print("Pushing env files to droplet...")
    scp_file(cfg.project_dir / ".env", f"{cfg.remote_dir}/.env", droplet_ip)
    relays_env = cfg.project_dir / ".env.relays"
    if relays_env.exists():
        scp_file(relays_env, f"{cfg.remote_dir}/.env.relays", droplet_ip)

    ensure_shared_network(droplet_ip)

    compose_env = cfg.compose_env()
    net_env = shared_network_compose_env()

    if not args.services:
        print(f"{'Rebuilding + restarting' if build else 'Restarting'} all services...")
        ssh_cmd(droplet_ip,
                f"cd {cfg.remote_dir} && {compose_env}{net_env}"
                f"COMPOSE_PROFILES='{profiles}' "
                f"docker compose {compose_files}up -d {build}--force-recreate")
    else:
        services: list[str] = []
        for name in args.services:
            svc = cfg.service_map.get(name)
            if not svc:
                valid = ", ".join(sorted(set(cfg.service_map.keys())))
                die(f"Unknown service: {name}\nValid names: {valid}")
            services.append(svc)

        svc_str = " ".join(services)
        print(f"{'Rebuilding + restarting' if build else 'Restarting'}: {svc_str}...")
        ssh_cmd(droplet_ip,
                f"cd {cfg.remote_dir} && {compose_env}{net_env}"
                f"COMPOSE_PROFILES='{profiles}' "
                f"docker compose {compose_files}up -d {build}--force-recreate {svc_str}")

    if args.local_files:
        post_deploy_sanity_check(droplet_ip, skip_flag=args.skip_post_check)

    print("Done.")
