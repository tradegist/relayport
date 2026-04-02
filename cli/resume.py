import os
import subprocess
import time

from cli import (
    load_env, env, validate_poller_env, do_api,
    ssh_cmd, scp_file, droplet_size_for_heap, die, PROJECT_DIR,
)


def run(args):
    state_file = PROJECT_DIR / ".pause-state"

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

    profiles = ""
    if validate_poller_env("_2"):
        profiles = "poller2"

    heap = int(env("JAVA_HEAP_SIZE", "768"))
    droplet_size = droplet_size_for_heap(heap)

    print(f"Resuming from snapshot: {snapshot_name} ({snapshot_id})")
    print(f"  Region: {region}")
    print(f"  Reserved IP: {reserved_ip}")
    print(f"  Droplet size: {droplet_size} (heap {heap}MB)")

    # 1. Find SSH key on DigitalOcean
    print("Looking up SSH key...")
    data = do_api("GET", "/account/keys")
    keys = [k for k in data["ssh_keys"] if "ibkr-relay" in k["name"].lower()]
    ssh_keys_param = []
    if keys:
        ssh_keys_param = [keys[0]["id"]]
        print(f"  SSH key ID: {keys[0]['id']}")
    else:
        print("  Warning: No 'ibkr-relay' SSH key found on DigitalOcean.")
        print("  You may need to add your SSH key manually after creation.")

    # 2. Create droplet from snapshot
    print("Creating droplet from snapshot...")
    data = do_api("POST", "/droplets", {
        "name": "ibkr-relay",
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
    env_file = PROJECT_DIR / ".env"
    for _ in range(10):
        try:
            scp_file(env_file, "/opt/ibkr-relay/.env", reserved_ip, strict_host_check=False)
            break
        except subprocess.CalledProcessError:
            time.sleep(5)
    else:
        die("Could not push .env to droplet after 10 attempts.")

    compose_cmd = (
        f"cd /opt/ibkr-relay && COMPOSE_PROFILES='{profiles}' "
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

    vnc_domain = env("VNC_DOMAIN", "vnc.example.com")

    print()
    print("=" * 44)
    print("  Resumed successfully!")
    print("=" * 44)
    print()
    print(f"  Droplet ID:   {droplet_id}")
    print(f"  Reserved IP:  {reserved_ip}")
    print("  Snapshot deleted (no longer billed)")
    print()
    print("  Next steps:")
    print(f"  1. Open https://{vnc_domain} to complete 2FA")
    print("  2. The poller will resume automatically")
    print()
    print("=" * 44)
