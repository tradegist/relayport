import os
import shutil

from cli import (
    load_env, require_env, env, compose_profiles,
    terraform, die, PROJECT_DIR,
)


def run(args):
    for cmd in ["terraform", "curl"]:
        if not shutil.which(cmd):
            die(f"'{cmd}' is required but not installed.")

    load_env()

    require_env(
        "DO_API_TOKEN", "TWS_USERID", "TWS_PASSWORD",
        "VNC_SERVER_PASSWORD", "WEBHOOK_SECRET",
        "IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID",
    )

    # Export TF_VAR_* for Terraform
    tf = {
        "do_token": env("DO_API_TOKEN"),
        "tws_userid": env("TWS_USERID"),
        "tws_password": env("TWS_PASSWORD"),
        "trading_mode": env("TRADING_MODE", "paper"),
        "vnc_password": env("VNC_SERVER_PASSWORD"),
        "webhook_url": env("TARGET_WEBHOOK_URL", ""),
        "webhook_secret": env("WEBHOOK_SECRET"),
        "flex_token": env("IBKR_FLEX_TOKEN"),
        "flex_query_id": env("IBKR_FLEX_QUERY_ID"),
        "poll_interval": env("POLL_INTERVAL_SECONDS", "600"),
        "time_zone": env("TIME_ZONE", "America/New_York"),
        "java_heap_size": env("JAVA_HEAP_SIZE", "768"),
        # Poller-2 (optional)
        "flex_token_2": env("IBKR_FLEX_TOKEN_2", ""),
        "flex_query_id_2": env("IBKR_FLEX_QUERY_ID_2", ""),
        "webhook_url_2": env("TARGET_WEBHOOK_URL_2", ""),
        "webhook_secret_2": env("WEBHOOK_SECRET_2", ""),
        "poll_interval_2": env("POLL_INTERVAL_SECONDS_2", "600"),
    }
    for key, val in tf.items():
        os.environ[f"TF_VAR_{key}"] = val

    # Validate poller-2 config (sets COMPOSE_PROFILES if configured)
    compose_profiles()

    terraform("init", "-input=false")
    terraform("apply", "-auto-approve", "-input=false")

    droplet_ip = terraform("output", "-raw", "droplet_ip", capture=True).stdout.strip()
    vnc_url = terraform("output", "-raw", "vnc_url", capture=True).stdout.strip()

    print()
    print("=" * 44)
    print("  Deployment complete!")
    print("=" * 44)
    print()
    print(f"  Droplet IP:  {droplet_ip}")
    print(f"  VNC URL:     {vnc_url}")
    print()
    print("  Next steps:")
    print("  1. Open the VNC URL in your browser")
    print("  2. Complete the IBKR 2FA handshake")
    print("  3. The relay will start listening for order fills")
    print()
