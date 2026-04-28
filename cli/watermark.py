import subprocess

from cli import REMOTE_DIR, get_relay_env
from cli.core import die, env, load_env, ssh_cmd


def _build_script(relay_names: list[str]) -> str:
    """Build a Python one-liner to run inside the relays container.

    Uses double-quoted strings only — script is wrapped in single quotes on SSH.

    Key logic: the union of (a) existing watermark keys in the DB and (b) index-0
    keys derived from the RELAYS env var ensures watermarks are created even when
    the metadata table is empty (e.g. after reset-db or on first run with no fills).
    """
    f_repr = "{" + ", ".join(f'"{n}"' for n in relay_names) + "}" if relay_names else "None"
    return (
        "import sqlite3, time, os; "
        "from relay_core.poller_engine import META_DB_PATH; "
        f"relay_filter = {f_repr}; "
        "now = int(time.time()); "
        "conn = sqlite3.connect(META_DB_PATH); "
        # Existing watermark keys (covers multi-account indices already in the DB)
        'rows = conn.execute("SELECT key FROM metadata").fetchall(); '
        'existing = {r[0] for r in rows if r[0].endswith(":last_poll_ts")}; '
        # Index-0 keys derived from the RELAYS env var (creates entries even when DB is empty)
        'configured = [r.strip() for r in os.environ.get("RELAYS", "").split(",") if r.strip()]; '
        'expected = {r + ":last_poll_ts" for r in configured}; '
        # Union, then apply relay_filter
        'keys = [k for k in existing | expected if relay_filter is None or k.split(":")[0] in relay_filter]; '
        '[conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (k, str(now))) for k in keys]; '
        "conn.commit(); conn.close(); "
        '[print("  " + k + " -> " + str(now)) for k in keys]; '
        'print(str(len(keys)) + " watermark(s) reset") if keys else print("No watermark keys found")'
    )


def run(args) -> None:
    load_env()
    relay_names: list[str] = getattr(args, "relays", None) or []
    relay_env = get_relay_env()
    is_local = relay_env == "local"
    target = "local" if is_local else "prod"

    label = ", ".join(relay_names) if relay_names else "all relays"
    print(f"Resetting watermark to now for {label} [{target}]...")

    script = _build_script(relay_names)

    if is_local:
        cmd = [
            "docker", "compose",
            "-f", "docker-compose.yml",
            "-f", "docker-compose.local.yml",
            "exec", "-T", "relays",
            "python3", "-c", script,
        ]
        result = subprocess.run(cmd, text=True)
        if result.returncode != 0:
            die("Command failed — is the local stack running? (make local-up)")
    else:
        ip = env("DROPLET_IP")
        remote_cmd = (
            f"cd {REMOTE_DIR} && "
            f"docker compose exec -T relays python3 -c '{script}'"
        )
        ssh_cmd(ip, remote_cmd)
