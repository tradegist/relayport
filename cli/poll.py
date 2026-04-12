import json
import subprocess
import sys
import time

from cli import REMOTE_DIR, get_relay_env, relay_api
from cli.core import die, env, load_env, ssh_key_path


def _start_log_tail(is_local: bool) -> subprocess.Popen[str]:
    """Start tailing relays container logs in the background."""
    if is_local:
        cmd = [
            "docker", "compose",
            "-f", "docker-compose.yml",
            "-f", "docker-compose.local.yml",
            "logs", "-f", "--tail=0", "relays",
        ]
    else:
        ip = env("DROPLET_IP")
        remote = f"cd {REMOTE_DIR} && docker compose logs -f --tail=0 relays"
        cmd = ["ssh", "-i", ssh_key_path(), f"root@{ip}", remote]
    return subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)


def run(args):
    load_env()

    relay = getattr(args, "relay", "ibkr")
    poll_idx = getattr(args, "poll_idx", "1")
    replay = getattr(args, "replay", None)
    verbose = getattr(args, "verbose", False) or replay is not None

    try:
        idx_int = int(poll_idx)
        if idx_int < 1:
            raise ValueError
    except ValueError:
        die(f"Invalid poll index: {poll_idx!r} — must be a positive integer")

    endpoint = f"/relays/{relay}/poll/{poll_idx}"
    relay_env = get_relay_env()
    is_local = relay_env == "local"

    label = f"{relay}"
    if idx_int > 1:
        label += f" #{poll_idx}"
    target = "local" if is_local else "prod"
    print(f"Triggering immediate poll ({label}) [{target}]...")

    log_proc = None
    try:
        if verbose:
            log_proc = _start_log_tail(is_local)
            time.sleep(0.5)

        body = {"replay": replay} if replay is not None else None
        data = relay_api(endpoint, data=body)

        if verbose:
            time.sleep(1)
            print()
        print(json.dumps(data, indent=4))
    finally:
        if log_proc is not None:
            log_proc.terminate()
            log_proc.wait()
