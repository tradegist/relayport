from cli import (
    load_env, env, validate_poller_env,
    ssh_cmd, scp_file, die, PROJECT_DIR,
)

SERVICE_MAP = {
    "gateway": "ib-gateway",
    "ib-gateway": "ib-gateway",
    "novnc": "novnc",
    "vnc": "novnc",
    "caddy": "caddy",
    "relay": "webhook-relay",
    "webhook-relay": "webhook-relay",
    "poller": "poller",
    "poller2": "poller-2",
    "poller-2": "poller-2",
}


def run(args):
    load_env()

    droplet_ip = env("DROPLET_IP")
    profiles = ""
    if validate_poller_env("_2"):
        profiles = "poller2"

    print("Pushing .env to droplet...")
    scp_file(PROJECT_DIR / ".env", "/opt/ibkr-relay/.env", droplet_ip)

    if not args.services:
        print("Restarting all services...")
        ssh_cmd(droplet_ip,
                f"cd /opt/ibkr-relay && COMPOSE_PROFILES='{profiles}' "
                "docker compose up -d --force-recreate")
    else:
        services = []
        for name in args.services:
            svc = SERVICE_MAP.get(name)
            if not svc:
                valid = ", ".join(sorted(set(SERVICE_MAP.keys())))
                die(f"Unknown service: {name}\nValid names: {valid}")
            services.append(svc)

        svc_str = " ".join(services)
        print(f"Restarting: {svc_str}...")
        ssh_cmd(droplet_ip,
                f"cd /opt/ibkr-relay && COMPOSE_PROFILES='{profiles}' "
                f"docker compose up -d --force-recreate {svc_str}")

    print("Done.")
