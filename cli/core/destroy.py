import os
import shutil

from cli.core import config, die, load_env, require_env, terraform


def run(args):
    if not shutil.which("terraform"):
        die("'terraform' is required but not installed.")

    load_env()
    cfg = config()

    if not os.environ.get("DO_API_TOKEN"):
        die("DO_API_TOKEN is not set in .env.droplet")
    require_env("SITE_DOMAIN")

    # Export TF_VAR_* only when the source env var is present/non-empty.
    # Leaving TF_VAR_* unset lets Terraform defaults and validation behave correctly.
    for tf_name, env_key in cfg.terraform_vars.items():
        tf_var_key = f"TF_VAR_{tf_name}"
        env_value = os.environ.get(env_key)
        if env_value:
            os.environ[tf_var_key] = env_value
        else:
            os.environ.pop(tf_var_key, None)

    # Capture the reserved IP before touching state, so we can remind the user.
    try:
        reserved_ip = terraform("output", "-raw", "droplet_ip", capture=True).stdout.strip()
    except Exception:
        reserved_ip = ""

    # Remove the reserved IP from Terraform state so destroy does not delete it
    # from the DO account. It stays as an unassigned reserved IP and can be
    # re-imported on the next deploy via DROPLET_IP in .env.droplet.
    # Wrapped in try/except: if state is missing or uninitialized, proceed
    # with destroy anyway rather than blocking on a state inspection failure.
    try:
        state = terraform("state", "list", capture=True).stdout
        if "digitalocean_reserved_ip.relay" in state:
            print("Preserving reserved IP (removing from Terraform state)...")
            terraform("state", "rm", "digitalocean_reserved_ip.relay")
    except Exception as exc:
        print("WARNING: Failed to preserve the reserved IP in Terraform state before destroy.")
        print(f"  Terraform state operation failed: {exc}")
        print("  Continuing with destroy may allow Terraform to delete the reserved IP.")
        print("  Check Terraform state/lock/permissions and intervene before running destroy again.")

    terraform("destroy", "-auto-approve", "-input=false")

    print()
    print("Infrastructure destroyed.")
    if reserved_ip:
        print()
        print(f"  Reserved IP {reserved_ip} preserved on your DO account.")
        print(f"  To reuse it, set DROPLET_IP={reserved_ip} in .env.droplet before the next deploy.")
