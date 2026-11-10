variable "location" {
  description = "Azure region. The subscription policy admits only a few regions; indiasouthcentral is the one with an allowed 2-vCPU size and quota (docs/DECISIONS.md D92)."
  type        = string
  default     = "indiasouthcentral"

  validation {
    condition     = var.location == "indiasouthcentral"
    error_message = "The authorised scope is the single region indiasouthcentral."
  }
}

variable "vm_size" {
  description = "The 2-vCPU, 8 GB size checked as allowed, with quota, in indiasouthcentral."
  type        = string
  default     = "Standard_D2as_v4"

  validation {
    condition     = var.vm_size == "Standard_D2as_v4"
    error_message = "Only Standard_D2as_v4 (2 vCPU, 8 GB) is authorised for the demo VM."
  }
}

variable "admin_username" {
  type    = string
  default = "vmadmin"
}

variable "ssh_public_key_path" {
  description = "Path to the SSH PUBLIC key (the private key never leaves this machine)."
  type        = string
  default     = "~/.ssh/vitalmesh_azure.pub"
}

variable "ssh_allowed_cidr" {
  description = "The one address allowed to open SSH, as a /32."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{1,3}(\\.[0-9]{1,3}){3}/32$", var.ssh_allowed_cidr))
    error_message = "Give a single address as a /32, never a wider range and never 0.0.0.0/0."
  }
}

variable "dns_label" {
  description = "The first part of the public name: <label>.indiasouthcentral.cloudapp.azure.com."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,38}[a-z0-9]$", var.dns_label))
    error_message = "3-40 characters: lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "delete_by" {
  description = "The date this deployment is to be deleted (a tag, so anyone looking at the portal sees it), for example 2026-11-14."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", var.delete_by))
    error_message = "Use YYYY-MM-DD."
  }
}
