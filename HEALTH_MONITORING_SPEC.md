# Health Monitoring & Alerting — Implementation Spec

## Goal

Detect when the relay system is degraded or down and push a notification so we can intervene before missing trades. Minimal moving parts — no extra infrastructure, no paid services.

---

## 1. Internal Health Endpoint

### 1a. Extend `GET /health` on webhook-relay (port 5000)

Current response: `{"connected": bool}`

New response:

```json
{
  "status": "ok | degraded | down",
  "ib_gateway_connected": true,
  "uptime_seconds": 3842,
  "last_order_at": "2026-04-02T14:23:11Z",
  "timestamp": "2026-04-02T15:05:00Z"
}
```

- `status`: `ok` if connected, `degraded` if reconnecting, `down` if disconnected > 60s
- `ib_gateway_connected`: current `ib.isConnected()` value
- `uptime_seconds`: seconds since the process started
- `last_order_at`: ISO timestamp of last successful order placement (null if none)
- `timestamp`: current server time

**File**: `remote-client/client.py`

### 1b. Add `GET /health` on poller (port 8000)

Currently the poller has no health endpoint — only `POST /ibkr/poller/run`.

Add a `GET /health` handler returning:

```json
{
  "status": "ok | error",
  "last_poll_at": "2026-04-02T15:00:00Z",
  "last_poll_success": true,
  "last_poll_fills": 3,
  "next_poll_in_seconds": 287,
  "db_size_bytes": 28672,
  "timestamp": "2026-04-02T15:05:00Z"
}
```

- `status`: `ok` if last poll succeeded, `error` if last poll failed
- `last_poll_at`: ISO timestamp of last poll attempt
- `last_poll_success`: bool — did the last Flex fetch + webhook delivery succeed
- `last_poll_fills`: number of new fills found in last poll (0 is fine, just informational)
- `next_poll_in_seconds`: seconds until the next scheduled poll
- `db_size_bytes`: SQLite file size (quick staleness/corruption indicator)

**File**: `poller/poller.py`

### 1c. Update Caddyfile routing

Add route so `GET {TRADE_DOMAIN}/health/poller` → `poller:8000/health`  
Keep `GET {TRADE_DOMAIN}/health` → `webhook-relay:5000/health` (already works via default route)

**File**: `caddy/Caddyfile`

---

## 2. Docker Healthchecks

Add `healthcheck` directives to `docker-compose.yml` for the three critical services:

```yaml
webhook-relay:
  healthcheck:
    test:
      [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')",
      ]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 30s

poller:
  healthcheck:
    test:
      [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')",
      ]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 30s

ib-gateway:
  healthcheck:
    test: ["CMD-SHELL", "nc -z localhost 4004 || nc -z localhost 4003"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 60s
```

This gives `docker ps` a HEALTH column and enables `docker compose ps` to show unhealthy containers.

**File**: `docker-compose.yml`

---

## 3. Alerting via ntfy.sh

Use [ntfy.sh](https://ntfy.sh) — free, no account required, push notifications to phone via topic URL.

### 3a. Add a watchdog script: `healthcheck.sh`

Runs on the **droplet** via cron. Hits the local health endpoints and sends an alert on failure.

```
Logic:
1.  curl -sf http://localhost:5000/health → parse JSON → check ib_gateway_connected == true
2.  curl -sf http://localhost:8000/health → parse JSON → check last_poll_success == true
3.  docker ps --filter "health=unhealthy" --format '{{.Names}}' → check for any unhealthy containers
4.  If any check fails → POST to ntfy.sh/<NTFY_TOPIC> with failure details
5.  On recovery (previous run failed, this run passed) → POST "recovered" message
```

State file: `/tmp/ibkr-health-state` (tracks last status to detect recovery)

Environment variable in `.env`: `NTFY_TOPIC` — a random unique topic name (e.g. `ibkr-relay-a7x9k2`)

**File**: `healthcheck.sh`

### 3b. Cron schedule

Install via cloud-init or deploy script:

```
*/5 * * * * /opt/ibkr-relay/healthcheck.sh >> /var/log/ibkr-healthcheck.log 2>&1
```

Runs every 5 minutes. Worst-case detection time: 5 minutes.

### 3c. Deploy integration

Update `sync-env.sh` or the deploy Makefile target to:

1. Copy `healthcheck.sh` to the droplet
2. Install the cron entry if not present

---

## 4. External Uptime Ping (Optional, Zero-Code)

Register `https://trade.example.com/health` on [UptimeRobot](https://uptimerobot.com) free tier:

- Check interval: 5 minutes
- Alert: email (free) or webhook to ntfy.sh
- Catches: droplet down, Caddy down, DNS issues, cert expiry

This is independent of the above and provides an outside-in view. No code changes needed — just a manual signup step.

---

## 5. Makefile Target

```makefile
health:  ## Check health of all services
	@echo "=== Relay ===" && curl -sf https://$$(grep TRADE_DOMAIN .env | cut -d= -f2)/health | python3 -m json.tool
	@echo "\n=== Poller ===" && curl -sf https://$$(grep TRADE_DOMAIN .env | cut -d= -f2)/health/poller | python3 -m json.tool
```

**File**: `Makefile`

---

## 6. Files Changed (Summary)

| File                      | Change                                                       |
| ------------------------- | ------------------------------------------------------------ |
| `remote-client/client.py` | Extend `/health` response with status, uptime, last_order_at |
| `poller/poller.py`        | Add `GET /health` handler with poll status                   |
| `caddy/Caddyfile`         | Add `/health/poller` route                                   |
| `docker-compose.yml`      | Add healthcheck to 3 services                                |
| `healthcheck.sh`          | New — watchdog script for cron                               |
| `Makefile`                | Add `health` target                                          |
| `.env.example`            | Add `NTFY_TOPIC` variable                                    |
| `README.md`               | Document health monitoring setup, ntfy.sh, `make health`     |

---

## 7. What This Does NOT Cover

- **IB Gateway 2FA expiry detection** — IB Gateway disconnects daily and needs manual 2FA via noVNC. The healthcheck will alert on this (gateway disconnected), but can't auto-resolve it.
- **Trade execution monitoring** — Verifying that orders actually filled correctly is a business-logic concern, not infrastructure health.
- **Log aggregation** — No centralized logging. Use `make logs S=<service>` for ad-hoc debugging.
