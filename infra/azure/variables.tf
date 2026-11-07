variable "location" {
  description = "Azure region. The authorised scope is ONE region with an allowed 2-vCPU size and quota. The subscription policy only admits malaysiawest, indiasouthcentral, koreacentral, eastasia and uaenorth (eastus is refused, Central India has no usable size); indiasouthcentral works."
  type        = string
  default     = "indiasouthcentral"

  validation {
    condition     = var.location == "indiasouthcentral"
    error_message = "The authorised scope is the single region indiasouthcentral (docs/DECISIONS.md D92)."
  }
}

variable "vm_sizes" {
  description = "One 2-vCPU size per VM, in order. The student quota is 4 vCPUs per VM family, so 3 VMs need two families, for example [Standard_D2as_v4, Standard_D2as_v4, Standard_F2s_v2]."
  type        = list(string)

  validation {
    condition     = length(var.vm_sizes) >= 1 && length(var.vm_sizes) <= 3 && alltrue([for s in var.vm_sizes : contains(["Standard_D2as_v4", "Standard_F2s_v2"], s)])
    error_message = "Give 1 to 3 sizes, each Standard_D2as_v4 or Standard_F2s_v2 (the 2-vCPU sizes checked as allowed, with quota, in indiasouthcentral)."
  }
}

variable "vm_count" {
  description = "How many VMs (1 to 3). The student quota is 6 regional vCPUs, so 3 x 2 vCPU is the most."
  type        = number
  default     = 1

  validation {
    condition     = var.vm_count >= 1 && var.vm_count <= 3
    error_message = "The authorised scope is at most 3 VMs of 2 vCPU each."
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
  description = "The one address allowed to open SSH, as a /32 (for example 203.0.113.7/32)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{1,3}(\\.[0-9]{1,3}){3}/32$", var.ssh_allowed_cidr))
    error_message = "Give a single address as a /32, never a wider range and never 0.0.0.0/0."
  }
}

variable "enable_keyvault" {
  description = "Create a Key Vault in the group (role-based access, VMs may read secrets)."
  type        = bool
  default     = false
}
