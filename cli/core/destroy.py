import os
import shutil

from cli.core import config, die, env, load_env, terraform


def run(args):
    if not shutil.which("terraform"):
        die("'terraform' is required but not installed.")

    load_env()
    cfg = config()

    if not os.environ.get("DO_API_TOKEN"):
        die("DO_API_TOKEN is not set in .env")

    # Terraform needs all required variables even for destroy.
    # Resolve each var from env with a "placeholder" fallback.
    for tf_name, env_key in cfg.terraform_vars.items():
        os.environ[f"TF_VAR_{tf_name}"] = env(env_key, "placeholder")

    # Capture the reserved IP before touching state, so we can remind the user.
    try:
        reserved_ip = terraform("output", "-raw", "droplet_ip", capture=True).stdout.strip()
    except Exception:
        reserved_ip = ""

    # Remove the reserved IP from Terraform state so destroy does not delete it
    # from the DO account. It stays as an unassigned reserved IP and can be
    # re-imported on the next deploy via RESERVED_IP in .env.droplet.
    state = terraform("state", "list", capture=True).stdout
    if "digitalocean_reserved_ip.relay" in state:
        print("Preserving reserved IP (removing from Terraform state)...")
        terraform("state", "rm", "digitalocean_reserved_ip.relay")

    terraform("destroy", "-auto-approve", "-input=false")

    print()
    print("Infrastructure destroyed.")
    if reserved_ip:
        print()
        print(f"  Reserved IP {reserved_ip} preserved on your DO account.")
        print(f"  DROPLET_IP in .env.droplet already holds this value — next deploy will reuse it.")
