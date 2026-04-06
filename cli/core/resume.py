import os
import subprocess
import time

from cli.core import config, die, do_api, env, load_env, scp_file, ssh_cmd


def run(args):
    cfg = config()
    state_file = cfg.project_dir / ".pause-state"

    if not state_file.exists():
        die(".pause-state not found — nothing to resume.\n"
            "Run 'python3 -m cli pause' first to create a snapshot.")

    load_env()

    # Parse pause state
    state = {}
    for line in state_file.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            state[k.strip()] = v.strip()

    snapshot_id = state["SNAPSHOT_ID"]
    snapshot_name = state["SNAPSHOT_NAME"]
    reserved_ip = state["RESERVED_IP"]
    region = state["DROPLET_REGION"]

    profiles = cfg.compose_profiles()
    droplet_size = cfg.droplet_size()

    print(f"Resuming from snapshot: {snapshot_name} ({snapshot_id})")
    print(f"  Region: {region}")
    print(f"  Reserved IP: {reserved_ip}")
    print(f"  Droplet size: {droplet_size}")

    # 1. Find SSH key on DigitalOcean
    print("Looking up SSH key...")
    data = do_api("GET", "/account/keys")
    keys = [k for k in data["ssh_keys"] if cfg.project_name in k["name"].lower()]
    ssh_keys_param = []
    if keys:
        ssh_keys_param = [keys[0]["id"]]
        print(f"  SSH key ID: {keys[0]['id']}")
    else:
        print(f"  Warning: No '{cfg.project_name}' SSH key found on DigitalOcean.")
        print("  You may need to add your SSH key manually after creation.")

    # 2. Create droplet from snapshot
    print("Creating droplet from snapshot...")
    data = do_api("POST", "/droplets", {
        "name": cfg.project_name,
        "region": region,
        "size": droplet_size,
        "image": int(snapshot_id),
        "ssh_keys": ssh_keys_param,
    })
    droplet_id = data["droplet"]["id"]
    if not droplet_id:
        die("Failed to create droplet.")
    print(f"  Droplet ID: {droplet_id}")

    print("  Waiting for droplet to boot...")
    for _ in range(60):
        data = do_api("GET", f"/droplets/{droplet_id}")
        if data["droplet"]["status"] == "active":
            break
        time.sleep(3)
    else:
        die("Droplet did not become active in time.")
    print("  Droplet is active.")

    # 3. Assign reserved IP
    print(f"Assigning reserved IP {reserved_ip}...")
    do_api("POST", f"/reserved_ips/{reserved_ip}/actions", {
        "type": "assign",
        "droplet_id": droplet_id,
    })
    time.sleep(5)

    # 4. Sync .env and restart
    print("Syncing .env and restarting containers...")
    env_file = cfg.project_dir / ".env"
    for _ in range(10):
        try:
            scp_file(env_file, f"{cfg.remote_dir}/.env", reserved_ip, strict_host_check=False)
            break
        except subprocess.CalledProcessError:
            time.sleep(5)
    else:
        die("Could not push .env to droplet after 10 attempts.")

    compose_cmd = (
        f"cd {cfg.remote_dir} && COMPOSE_PROFILES='{profiles}' "
        "docker compose up -d --force-recreate"
    )
    result = ssh_cmd(reserved_ip, compose_cmd, strict_host_check=False, capture=True)
    for line in result.stdout.strip().splitlines()[-5:]:
        print(f"  {line}")

    # 5. Delete snapshot
    print(f"Deleting snapshot {snapshot_id}...")
    do_api("DELETE", f"/snapshots/{snapshot_id}")

    # 6. Clean up
    state_file.unlink()

    print()
    print("=" * 44)
    print("  Resumed successfully!")
    print("=" * 44)
    print()
    print(f"  Droplet ID:   {droplet_id}")
    print(f"  Reserved IP:  {reserved_ip}")
    print("  Snapshot deleted (no longer billed)")
    if cfg.post_resume_message:
        print()
        msg = cfg.post_resume_message.format_map(os.environ)
        print(f"  {msg}")
    print()
    print("=" * 44)
