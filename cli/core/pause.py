import time
from datetime import datetime

from cli.core import config, die, do_api, env, load_env


def run(args):
    cfg = config()
    state_file = cfg.project_dir / ".pause-state"

    if state_file.exists():
        die(".pause-state already exists — environment is already paused.\n"
            "Run 'python3 -m cli resume' first, or delete .pause-state if stale.")

    load_env()

    reserved_ip = env("DROPLET_IP")

    # 1. Find droplet ID from reserved IP
    print(f"Looking up droplet assigned to {reserved_ip}...")
    data = do_api("GET", f"/reserved_ips/{reserved_ip}")
    droplet = data.get("reserved_ip", {}).get("droplet")
    if not droplet:
        die(f"No droplet is assigned to reserved IP {reserved_ip}")
    droplet_id = droplet["id"]
    print(f"  Droplet ID: {droplet_id}")

    # 2. Power off
    print("Powering off droplet...")
    do_api("POST", f"/droplets/{droplet_id}/actions", {"type": "power_off"})

    for _ in range(30):
        data = do_api("GET", f"/droplets/{droplet_id}")
        if data["droplet"]["status"] == "off":
            print("  Droplet is off.")
            break
        time.sleep(2)
    else:
        die("Droplet did not power off in time.")

    # 3. Create snapshot
    snap_name = f"{cfg.project_name}-pause-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    print(f"Creating snapshot: {snap_name}...")
    data = do_api("POST", f"/droplets/{droplet_id}/actions",
                  {"type": "snapshot", "name": snap_name})
    action_id = data["action"]["id"]

    print("  Waiting for snapshot (this may take a few minutes)...")
    for _ in range(120):
        data = do_api("GET", f"/actions/{action_id}")
        if data["action"]["status"] == "completed":
            break
        time.sleep(5)
    else:
        die("Snapshot did not complete in time.")

    # Get snapshot ID
    data = do_api("GET", f"/droplets/{droplet_id}/snapshots")
    snapshots = [s for s in data["snapshots"] if s["name"] == snap_name]
    if not snapshots:
        die("Could not find snapshot ID.")
    snapshot_id = snapshots[0]["id"]
    print(f"  Snapshot ID: {snapshot_id}")

    # 4. Unassign reserved IP
    print("Unassigning reserved IP...")
    do_api("POST", f"/reserved_ips/{reserved_ip}/actions", {"type": "unassign"})
    time.sleep(3)

    # 5. Delete droplet
    print(f"Deleting droplet {droplet_id}...")
    do_api("DELETE", f"/droplets/{droplet_id}")

    # 6. Save state
    data = do_api("GET", f"/reserved_ips/{reserved_ip}")
    region = data["reserved_ip"]["region"]["slug"]

    state_file.write_text(
        f"SNAPSHOT_ID={snapshot_id}\n"
        f"SNAPSHOT_NAME={snap_name}\n"
        f"RESERVED_IP={reserved_ip}\n"
        f"DROPLET_REGION={region}\n"
        f"PAUSED_AT={datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
    )

    print()
    print("=" * 44)
    print("  Paused successfully!")
    print("=" * 44)
    print()
    print(f"  Snapshot: {snap_name} ({snapshot_id})")
    print(f"  Reserved IP: {reserved_ip} (kept, unassigned)")
    print("  State saved to: .pause-state")
    print()
    print("  Droplet billing has stopped.")
    print("  Snapshot cost: ~$0.06/GB/month")
    print("  Reserved IP: billed while unassigned")
    print()
    print("  To resume: python3 -m cli resume")
    print("=" * 44)
