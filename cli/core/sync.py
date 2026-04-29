import argparse
import shutil
import subprocess

from cli.core import config, die, env, is_shared, load_env, scp_file, ssh_cmd, ssh_key_path


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

    print("Running type checks...")
    subprocess.run(["make", "typecheck"], check=True, cwd=cfg.project_dir)

    print("Running linter...")
    subprocess.run(["make", "lint"], check=True, cwd=cfg.project_dir)

    print("Running unit tests...")
    subprocess.run(["make", "test"], check=True, cwd=cfg.project_dir)

    if skip_e2e:
        print("Skipping E2E tests (--skip-e2e)")
    else:
        print("Running E2E tests...")
        subprocess.run(["make", "e2e"], check=True, cwd=cfg.project_dir)


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

    build = "--build " if (args.build or args.local_files) else ""

    # Shared mode uses the shared compose overlay
    compose_files = ""
    if is_shared():
        compose_files = "-f docker-compose.yml -f docker-compose.shared.yml "

    print("Pushing env files to droplet...")
    scp_file(cfg.project_dir / ".env", f"{cfg.remote_dir}/.env", droplet_ip)
    relays_env = cfg.project_dir / ".env.relays"
    if relays_env.exists():
        scp_file(relays_env, f"{cfg.remote_dir}/.env.relays", droplet_ip)

    compose_env = cfg.compose_env()

    if not args.services:
        print(f"{'Rebuilding + restarting' if build else 'Restarting'} all services...")
        ssh_cmd(droplet_ip,
                f"cd {cfg.remote_dir} && {compose_env}COMPOSE_PROFILES='{profiles}' "
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
                f"cd {cfg.remote_dir} && {compose_env}COMPOSE_PROFILES='{profiles}' "
                f"docker compose {compose_files}up -d {build}--force-recreate {svc_str}")

    print("Done.")
