# Azure benchmark environment (D90): ONE resource group, up to 3 small Linux VMs, one subnet.
#
# Scope authorised by Sahil on 2026-11-07 (see docs/DECISIONS.md D90): resource group
# `vitalmesh-bench`, region indiasouthcentral (the subscription policy admits only five regions; eastus is refused, Central India has no usable size), at most 3 VMs of 2 vCPU each, about $15 of the $100
# student credit in total, everything deleted after each test (infra/azure/teardown.ps1), nothing
# left running overnight. This file cannot exceed the VM count (see the validation in variables.tf).
#
# Secrets and ids: nothing here holds a credential. The subscription comes from the signed-in Azure
# CLI session (ARM_SUBSCRIPTION_ID is set in the shell at run time, never written to a file); VMs
# take an SSH public key only (password login is disabled); SSH is open only to one address.
#
#   terraform init
#   terraform plan  -var-file=bench.tfvars   (vm_sizes, vm_count, enable_keyvault, ssh_allowed_cidr; keep it out of git)
#   terraform apply -var-file=bench.tfvars
#   pwsh infra/azure/teardown.ps1          # always, when the test is finished
#
# Optional (-var enable_keyvault=true): a Key Vault in the same group with role-based access. The
# VMs get a managed identity that may READ secrets; the operator may write them. The API then loads
# its signing secret from the vault at start (VITALMESH_AUTH_SECRET_KEYVAULT) instead of an env file.

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  # use the signed-in `az login` session; skip registering every resource provider (they are
  # registered one by one, deliberately: Microsoft.Compute and Microsoft.Network only)
  resource_provider_registrations = "none"
}

locals {
  name = "vitalmesh-bench"
  tags = {
    project = "vitalmesh"
    purpose = "benchmark"
    delete  = "after-each-test"
  }
}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "bench" {
  name     = local.name
  location = var.location
  tags     = local.tags
}

resource "azurerm_virtual_network" "bench" {
  name                = "${local.name}-vnet"
  location            = azurerm_resource_group.bench.location
  resource_group_name = azurerm_resource_group.bench.name
  address_space       = ["10.60.0.0/24"]
  tags                = local.tags
}

resource "azurerm_subnet" "bench" {
  name                 = "vms"
  resource_group_name  = azurerm_resource_group.bench.name
  virtual_network_name = azurerm_virtual_network.bench.name
  address_prefixes     = ["10.60.0.0/26"]
}

resource "azurerm_network_security_group" "bench" {
  name                = "${local.name}-nsg"
  location            = azurerm_resource_group.bench.location
  resource_group_name = azurerm_resource_group.bench.name
  tags                = local.tags

  # SSH from ONE address only (the person running the test)
  security_rule {
    name                       = "ssh-from-operator"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = var.ssh_allowed_cidr
    destination_address_prefix = "*"
  }

  # the HTTPS check of the API (a self-signed certificate, see docs/SCALING.md) from the same one address
  security_rule {
    name                       = "https-from-operator"
    priority                   = 105
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8443"
    source_address_prefix      = var.ssh_allowed_cidr
    destination_address_prefix = "*"
  }

  # the VMs talk to each other freely (Kafka, Schema Registry, Redis, the processors)
  security_rule {
    name                       = "within-subnet"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "10.60.0.0/26"
    destination_address_prefix = "10.60.0.0/26"
  }

  security_rule {
    name                       = "deny-everything-else-inbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "bench" {
  subnet_id                 = azurerm_subnet.bench.id
  network_security_group_id = azurerm_network_security_group.bench.id
}

# Only the first VM gets a public address (it is also the jump host to the others): one public IP
# costs money and widens the attack surface, three would do both for nothing.
resource "azurerm_public_ip" "jump" {
  name                = "${local.name}-jump-ip"
  location            = azurerm_resource_group.bench.location
  resource_group_name = azurerm_resource_group.bench.name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags
}

resource "azurerm_network_interface" "vm" {
  count               = var.vm_count
  name                = "${local.name}-nic-${count.index}"
  location            = azurerm_resource_group.bench.location
  resource_group_name = azurerm_resource_group.bench.name
  tags                = local.tags

  ip_configuration {
    name                          = "primary"
    subnet_id                     = azurerm_subnet.bench.id
    private_ip_address_allocation = "Static"
    private_ip_address            = cidrhost("10.60.0.0/26", 10 + count.index)
    public_ip_address_id          = count.index == 0 ? azurerm_public_ip.jump.id : null
  }
}

resource "azurerm_linux_virtual_machine" "vm" {
  count                           = var.vm_count
  name                            = "${local.name}-${count.index}"
  location                        = azurerm_resource_group.bench.location
  resource_group_name             = azurerm_resource_group.bench.name
  size                            = var.vm_sizes[count.index]
  admin_username                  = var.admin_username
  disable_password_authentication = true

  # lets a VM read secrets from the vault without any stored credential
  identity {
    type = "SystemAssigned"
  }
  network_interface_ids = [azurerm_network_interface.vm[count.index].id]
  custom_data           = base64encode(file("${path.module}/cloud-init.yaml"))
  tags                  = merge(local.tags, { role = count.index == 0 ? "broker-and-generator" : "processor" })

  admin_ssh_key {
    username   = var.admin_username
    public_key = file(var.ssh_public_key_path)
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "StandardSSD_LRS"
    disk_size_gb         = 64
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }
}

# ---------------------------------------------------------------------------------------------
# Key Vault (optional): where the signing secret lives instead of an environment file (D92)
# ---------------------------------------------------------------------------------------------
resource "azurerm_key_vault" "bench" {
  count                      = var.enable_keyvault ? 1 : 0
  name                       = "vmbench${substr(sha1(azurerm_resource_group.bench.id), 0, 10)}"
  location                   = azurerm_resource_group.bench.location
  resource_group_name        = azurerm_resource_group.bench.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 7 # the minimum; teardown.ps1 purges the deleted vault
  purge_protection_enabled   = false
  tags                       = local.tags
}

resource "azurerm_role_assignment" "operator_writes_secrets" {
  count                = var.enable_keyvault ? 1 : 0
  scope                = azurerm_key_vault.bench[0].id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "vms_read_secrets" {
  count                = var.enable_keyvault ? var.vm_count : 0
  scope                = azurerm_key_vault.bench[0].id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_linux_virtual_machine.vm[count.index].identity[0].principal_id
}
