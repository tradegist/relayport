import subprocess
import sys

from cli import REMOTE_DIR, get_relay_env
from cli.core import die, env, load_env, ssh_cmd, ssh_key_path

# Uses double-quoted SQL strings so the whole script can be wrapped in single quotes on SSH.
_RESET_SCRIPT = (
    "from relay_core.dedup import DEDUP_DB_PATH; "
    "from relay_core.poller_engine import META_DB_PATH; "
    "import sqlite3; "
    "c=sqlite3.connect(DEDUP_DB_PATH); "
    'c.execute("DROP TABLE IF EXISTS processed_fills"); c.commit(); c.close(); '
    "c=sqlite3.connect(META_DB_PATH); "
    'c.execute("DROP TABLE IF EXISTS metadata"); c.commit(); c.close(); '
    'print("Reset complete: dedup and meta tables dropped")'
)


def run(args):
    load_env()
    # Reads RELAY_ENV (set by make ENV=local) or falls back to DEFAULT_CLI_ENV.
    relay_env = get_relay_env()
    is_local = relay_env == "local"
    target = "local" if is_local else "prod"

    yes = getattr(args, "yes", False)
    if not yes:
        print(f"This will drop all dedup and metadata tables [{target}].")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    print(f"Resetting DB state [{target}]...")

    if is_local:
        cmd = [
            "docker", "compose",
            "-f", "docker-compose.yml",
            "-f", "docker-compose.local.yml",
            "exec", "-T", "relays",
            "python3", "-c", _RESET_SCRIPT,
        ]
        result = subprocess.run(cmd, text=True)
        if result.returncode != 0:
            die("Reset failed — is the local stack running? (make local-up)")
    else:
        ip = env("DROPLET_IP")
        remote_cmd = (
            f"cd {REMOTE_DIR} && "
            f"docker compose exec -T relays python3 -c '{_RESET_SCRIPT}'"
        )
        ssh_cmd(ip, remote_cmd)

    print("Done.")
