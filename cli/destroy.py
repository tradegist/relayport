import os
import shutil

from cli import load_env, env, die, terraform


def run(args):
    if not shutil.which("terraform"):
        die("'terraform' is required but not installed.")

    load_env()

    if not os.environ.get("DO_API_TOKEN"):
        die("DO_API_TOKEN is not set in .env")

    # Terraform needs all required variables even for destroy
    tf = {
        "do_token": env("DO_API_TOKEN"),
        "tws_userid": env("TWS_USERID", "placeholder"),
        "tws_password": env("TWS_PASSWORD", "placeholder"),
        "vnc_password": env("VNC_SERVER_PASSWORD", "placeholder"),
        "webhook_url": env("TARGET_WEBHOOK_URL", "placeholder"),
        "webhook_secret": env("WEBHOOK_SECRET", "placeholder"),
        "flex_token": env("IBKR_FLEX_TOKEN", "placeholder"),
        "flex_query_id": env("IBKR_FLEX_QUERY_ID", "placeholder"),
    }
    for key, val in tf.items():
        os.environ[f"TF_VAR_{key}"] = val

    terraform("destroy", "-auto-approve", "-input=false")

    print()
    print("Infrastructure destroyed.")
