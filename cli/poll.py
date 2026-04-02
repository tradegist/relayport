import json

from cli import load_env, validate_poller_env, relay_api, die


def run(args):
    load_env()

    poller = getattr(args, "poller", "1")

    if poller == "2":
        if not validate_poller_env("_2"):
            die("Poller 2 is not configured. Set IBKR_FLEX_TOKEN_2, "
                "IBKR_FLEX_QUERY_ID_2, TARGET_WEBHOOK_URL_2, and "
                "WEBHOOK_SECRET_2 in .env")
        endpoint = "/ibkr/run-poll-2"
        label = "poller-2"
    else:
        if not validate_poller_env(""):
            die("Poller is not configured. Set IBKR_FLEX_TOKEN, "
                "IBKR_FLEX_QUERY_ID, TARGET_WEBHOOK_URL, and "
                "WEBHOOK_SECRET in .env")
        endpoint = "/ibkr/run-poll"
        label = "poller"

    print(f"Triggering immediate poll ({label})...")
    data = relay_api(endpoint)
    print(json.dumps(data, indent=4))
