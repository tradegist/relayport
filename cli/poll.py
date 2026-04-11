import json
import subprocess

from cli import REMOTE_DIR, get_relay_env, relay_api
from cli.core import die, env, load_env, ssh_cmd


def run(args):
    load_env()

    relay = getattr(args, "relay", "ibkr")
    poll_idx = getattr(args, "poll_idx", "1")
    debug = getattr(args, "debug", False)
    replay = getattr(args, "replay", None)
    verbose = getattr(args, "verbose", False) or debug or replay is not None

    try:
        idx_int = int(poll_idx)
        if idx_int < 1:
            raise ValueError
    except ValueError:
        die(f"Invalid poll index: {poll_idx!r} — must be a positive integer")

    endpoint = f"/relays/{relay}/poll/{poll_idx}"
    service = "relays"
    relay_env = get_relay_env()
    is_local = relay_env == "local"

    label = f"{relay}"
    if idx_int > 1:
        label += f" #{poll_idx}"
    target = "local" if is_local else "prod"
    print(f"Triggering immediate poll ({label}) [{target}]...")

    if verbose:
        exec_cmd = ("python -m relay_core.main --once")
        if debug:
            exec_cmd += " --debug"
        if replay is not None:
            exec_cmd += f" --replay {replay}"

        if is_local:
            cmd = [
                "docker", "compose",
                "-f", "docker-compose.yml",
                "-f", "docker-compose.local.yml",
                "exec", service,
            ] + exec_cmd.split()
            subprocess.run(cmd, check=True, text=True)
        else:
            ip = env("DROPLET_IP")
            ssh_cmd(ip, f"cd {REMOTE_DIR} && docker compose exec {service} {exec_cmd}")
    else:
        body = {"replay": replay} if replay is not None else None
        data = relay_api(endpoint, data=body)
        print(json.dumps(data, indent=4))
