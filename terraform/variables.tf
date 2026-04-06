variable "do_token" {
  description = "DigitalOcean API token"
  type        = string
  sensitive   = true
}

variable "java_heap_size" {
  description = "IB Gateway Java heap size in MB (determines droplet size)"
  type        = string
  default     = "768"
}

variable "droplet_size" {
  description = "Override droplet size slug (e.g. s-1vcpu-512mb). When set, ignores java_heap_size for sizing."
  type        = string
  default     = ""
}

variable "droplet_region" {
  description = "DigitalOcean region for the droplet"
  type        = string
  default     = "nyc3"
}

variable "vnc_domain" {
  description = "Domain for HTTPS VNC access (must have DNS A record pointing to droplet)"
  type        = string
}

variable "site_domain" {
  description = "Domain for HTTPS API (must have DNS A record pointing to droplet)"
  type        = string
}
