output "droplet_ip" {
  description = "Reserved (static) public IP of the droplet"
  value       = digitalocean_reserved_ip.relay.ip_address
}

output "site_url" {
  description = "Site base URL"
  value       = "https://${var.site_domain}"
}

output "ssh_private_key" {
  description = "SSH private key for accessing the droplet (save to ~/.ssh/broker-relay)"
  value       = tls_private_key.deploy.private_key_openssh
  sensitive   = true
}
