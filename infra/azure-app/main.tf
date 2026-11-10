# VitalMesh demo VM (D99): ONE small Linux VM that runs the whole app for a short, announced period
# so that one named person can try it from a link, then everything is deleted (teardown.ps1).
#
# This is a separate Terraform root from infra/azure (the benchmark VMs), on purpose: different
# resource group, different purpose, and the benchmark scope stays untouched.
#
# Authority: Sahil's explicit go is needed for EVERY apply (resource group `vitalmesh-demo`, region
# indiasouthcentral, one 2-vCPU VM, a few days at most). Nothing here holds a credential: the
# subscription comes from the signed-in `az login` session (ARM_SUBSCRIPTION_ID is set in the shell
# at run time by deploy.ps1 and never written to a file), the VM accepts an SSH public key only,
# model-provider keys are copied over SSH after boot (never into Terraform state or custom data).
#
# What is open to the internet: ports 80 and 443 (Caddy: HTTPS certificate from Let's Encrypt for the
# Azure DNS name, then a shared password in front of the whole site). SSH is open to ONE address.
#
#   pwsh infra/azure-app/deploy.ps1            # plan only
#   pwsh infra/azure-app/deploy.ps1 -Apply     # create, install, start (needs the explicit go)
#   pwsh infra/azure-app/teardown.ps1          # delete everything, prove it is gone

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
  resource_provider_registrations = "none"
}

locals {
  name = "vitalmesh-demo"
  tags = {
    project   = "vitalmesh"
    purpose   = "demo-for-one-reviewer"
    delete_by = var.delete_by
  }
}

resource "azurerm_resource_group" "demo" {
  name     = local.name
  location = var.location
  tags     = local.tags
}

resource "azurerm_virtual_network" "demo" {
  name                = "${local.name}-vnet"
  location            = azurerm_resource_group.demo.location
  resource_group_name = azurerm_resource_group.demo.name
  address_space       = ["10.70.0.0/24"]
  tags                = local.tags
}

resource "azurerm_subnet" "demo" {
  name                 = "vm"
  resource_group_name  = azurerm_resource_group.demo.name
  virtual_network_name = azurerm_virtual_network.demo.name
  address_prefixes     = ["10.70.0.0/26"]
}

resource "azurerm_network_security_group" "demo" {
  name                = "${local.name}-nsg"
  location            = azurerm_resource_group.demo.location
  resource_group_name = azurerm_resource_group.demo.name
  tags                = local.tags

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

  # port 80: the certificate authority checks the domain here, and plain http is redirected to https
  security_rule {
    name                       = "http-for-certificate"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "https"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
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

resource "azurerm_subnet_network_security_group_association" "demo" {
  subnet_id                 = azurerm_subnet.demo.id
  network_security_group_id = azurerm_network_security_group.demo.id
}

# A static address with a free DNS name (<label>.<region>.cloudapp.azure.com): that name is what the
# HTTPS certificate is issued for, so the visitor sees no browser warning.
resource "azurerm_public_ip" "demo" {
  name                = "${local.name}-ip"
  location            = azurerm_resource_group.demo.location
  resource_group_name = azurerm_resource_group.demo.name
  allocation_method   = "Static"
  sku                 = "Standard"
  domain_name_label   = var.dns_label
  tags                = local.tags
}

resource "azurerm_network_interface" "demo" {
  name                = "${local.name}-nic"
  location            = azurerm_resource_group.demo.location
  resource_group_name = azurerm_resource_group.demo.name
  tags                = local.tags

  ip_configuration {
    name                          = "primary"
    subnet_id                     = azurerm_subnet.demo.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.demo.id
  }
}

resource "azurerm_linux_virtual_machine" "demo" {
  name                            = local.name
  location                        = azurerm_resource_group.demo.location
  resource_group_name             = azurerm_resource_group.demo.name
  size                            = var.vm_size
  admin_username                  = var.admin_username
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.demo.id]
  custom_data                     = base64encode(file("${path.module}/cloud-init.yaml"))
  tags                            = local.tags

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
