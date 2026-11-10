output "fqdn" {
  description = "The public name of the demo (HTTPS only)."
  value       = azurerm_public_ip.demo.fqdn
}

output "public_ip" {
  value = azurerm_public_ip.demo.ip_address
}

output "ssh" {
  description = "How to open a shell on the VM."
  value       = "ssh -i ~/.ssh/vitalmesh_azure ${var.admin_username}@${azurerm_public_ip.demo.fqdn}"
}
