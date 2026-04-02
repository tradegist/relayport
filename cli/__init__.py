import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
_UNSET = object()


def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def load_env(path=None):
    path = Path(path) if path else PROJECT_DIR / ".env"
    if not path.exists():
        die(".env file not found. Copy .env.example to .env and fill in your values.")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value


def env(key, default=_UNSET):
    val = os.environ.get(key)
    if val is None:
        if default is _UNSET:
            die(f"{key} is not set in .env")
        return default
    return val


def require_env(*keys):
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        die(f"Missing required vars in .env: {', '.join(missing)}")


def validate_poller_env(suffix=""):
    required = ["IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID", "TARGET_WEBHOOK_URL", "WEBHOOK_SECRET"]
    missing = []
    set_count = 0
    for var in required:
        full = f"{var}{suffix}"
        if os.environ.get(full):
            set_count += 1
        else:
            missing.append(full)
    if set_count == 0:
        return False
    if missing:
        die(f"Poller{suffix or ''} partially configured. Missing: {', '.join(missing)}")
    return True


def compose_profiles():
    profiles = []
    if validate_poller_env("_2"):
        profiles.append("poller2")
    return ",".join(profiles)


def ssh_key_path():
    return os.environ.get("SSH_KEY", str(Path.home() / ".ssh" / "ibkr-relay"))


def ssh_cmd(ip, command, strict_host_check=True, capture=False):
    cmd = ["ssh", "-i", ssh_key_path()]
    if not strict_host_check:
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5"]
    cmd += [f"root@{ip}", command]
    kwargs = {"check": True}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(cmd, **kwargs)


def scp_file(local_path, remote_path, ip, strict_host_check=True):
    cmd = ["scp", "-i", ssh_key_path()]
    if not strict_host_check:
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5"]
    cmd += [str(local_path), f"root@{ip}:{remote_path}"]
    return subprocess.run(cmd, check=True)


def do_api(method, path, data=None):
    token = env("DO_API_TOKEN")
    url = f"https://api.digitalocean.com/v2{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
            return json.loads(content) if content else None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        die(f"DO API error ({e.code} {method} {path}): {err_body}")


def relay_api(path, method="POST", data=None):
    domain = env("TRADE_DOMAIN")
    token = env("API_TOKEN")
    url = f"https://{domain}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        content = e.read().decode()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            die(f"Request failed ({e.code}): {content}")


def droplet_size_for_heap(heap_mb):
    heap = int(heap_mb)
    if heap <= 1024:
        return "s-1vcpu-2gb"
    elif heap <= 3072:
        return "s-2vcpu-4gb"
    elif heap <= 6144:
        return "s-4vcpu-8gb"
    else:
        return "s-8vcpu-16gb"


def terraform(*args, capture=False):
    cmd = ["terraform"] + list(args)
    kwargs = {"cwd": str(PROJECT_DIR / "terraform"), "check": True}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(cmd, **kwargs)
