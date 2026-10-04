#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Cloud-init script: Install Docker and prepare the project directory.
# This runs as root on first boot. NO SECRETS here — they are transferred
# separately by the CLI deploy command over SSH.
# ---------------------------------------------------------------------------

# Swap file — a safety net for the small droplet sizes. Without swap, a
# memory spike makes the kernel OOM-kill the largest process (IB Gateway's
# JVM when ibkr-bridge runs on the droplet) instead of paging idle memory
# out to disk.
SWAP_FILE=/swapfile
if ! swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
  if [ ! -f "$SWAP_FILE" ]; then
    fallocate -l 2G "$SWAP_FILE"
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE"
  fi
  swapon "$SWAP_FILE"
fi
grep -q "^$SWAP_FILE " /etc/fstab || echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
# Swap only under real memory pressure, keeping the JVM's working set in RAM.
echo "vm.swappiness=10" > /etc/sysctl.d/99-swappiness.conf
sysctl -p /etc/sysctl.d/99-swappiness.conf

# Install Docker via official apt repository (deterministic, auditable)
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable docker
systemctl start docker

# Create project directory — files arrive via rsync from the CLI
mkdir -p /opt/relayport

# Directory is ready — the CLI deploy command will:
# 1. Rsync project files
# 2. Transfer .env with secrets
# 3. Run docker compose up -d
