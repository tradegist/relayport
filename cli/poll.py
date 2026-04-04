import json

from cli import load_env, validate_poller_env, relay_api, env, ssh_cmd, die


def run(args):
    load_env()

    poller = getattr(args, "poller", "1")
    debug = getattr(args, "debug", False)
    replay = getattr(args, "replay", None)
    verbose = getattr(args, "verbose", False) or debug or replay is not None

    if poller == "2":
        if not validate_poller_env("_2"):
            die("Poller 2 is not configured. Set IBKR_FLEX_TOKEN_2, "
                "IBKR_FLEX_QUERY_ID_2, TARGET_WEBHOOK_URL_2, and "
                "WEBHOOK_SECRET_2 in .env")
        endpoint = "/ibkr/poller/2/run"
        service = "poller-2"
    else:
        if not validate_poller_env(""):
            die("Poller is not configured. Set IBKR_FLEX_TOKEN, "
                "IBKR_FLEX_QUERY_ID, TARGET_WEBHOOK_URL, and "
                "WEBHOOK_SECRET in .env")
        endpoint = "/ibkr/poller/run"
        service = "poller"

    print(f"Triggering immediate poll ({service})...")

    if verbose:
        ip = env("DROPLET_IP")
        cmd = f"cd /opt/ibkr-relay && docker compose exec {service} python poller.py --once"
        if debug:
            cmd += " --debug"
        if replay is not None:
            cmd += f" --replay {replay}"
        ssh_cmd(ip, cmd)
    else:
        body = {"replay": replay} if replay is not None else None
        data = relay_api(endpoint, data=body)
        print(json.dumps(data, indent=4))
