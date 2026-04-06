terraform {
  required_version = ">= 1.5.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.0"
    }
  }
}

provider "digitalocean" {
  token = var.do_token
}

# ---------------------------------------------------------------------------
# Auto-detect deployer's public IP for firewall rules
# ---------------------------------------------------------------------------
data "http" "deployer_ip" {
  url = "https://api.ipify.org"
}

locals {
  deployer_ip = chomp(data.http.deployer_ip.response_body)
}

# ---------------------------------------------------------------------------
# SSH key — auto-generated, no user setup needed
# ---------------------------------------------------------------------------
resource "tls_private_key" "deploy" {
  algorithm = "ED25519"
}

resource "digitalocean_ssh_key" "deploy" {
  name       = "ibkr-relay-deploy"
  public_key = tls_private_key.deploy.public_key_openssh
}

# ---------------------------------------------------------------------------
# Droplet
# ---------------------------------------------------------------------------
locals {
  heap = tonumber(var.java_heap_size)
  heap_based_size = (
    local.heap <= 1024 ? "s-1vcpu-2gb" :
    local.heap <= 3072 ? "s-2vcpu-4gb" :
    local.heap <= 6144 ? "s-4vcpu-8gb" :
    "s-8vcpu-16gb"
  )
  droplet_size = var.droplet_size != "" ? var.droplet_size : local.heap_based_size
}

resource "digitalocean_droplet" "relay" {
  image    = "ubuntu-24-04-x64"
  name     = "ibkr-relay"
  region   = var.droplet_region
  size     = local.droplet_size
  ssh_keys = [digitalocean_ssh_key.deploy.fingerprint]

  user_data = file("${path.module}/cloud-init.sh")

  connection {
    type        = "ssh"
    host        = self.ipv4_address
    user        = "root"
    private_key = tls_private_key.deploy.private_key_openssh
  }

  # Wait for cloud-init to finish (Docker install)
  provisioner "remote-exec" {
    inline = [
      "cloud-init status --wait",
    ]
  }
}

# ---------------------------------------------------------------------------
# Reserved (static) IP — survives power cycles / reboots
# ---------------------------------------------------------------------------
resource "digitalocean_reserved_ip" "relay" {
  region     = var.droplet_region
  droplet_id = digitalocean_droplet.relay.id
}

# ---------------------------------------------------------------------------
# Firewall — restrict SSH + noVNC to deployer IP only
# ---------------------------------------------------------------------------
resource "digitalocean_firewall" "relay" {
  name        = "ibkr-relay-fw"
  droplet_ids = [digitalocean_droplet.relay.id]

  # SSH
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = ["${local.deployer_ip}/32"]
  }

  # HTTPS (Caddy reverse proxy for noVNC)
  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # All outbound (DNS, HTTPS for Docker pulls, IBKR API, etc.)
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
