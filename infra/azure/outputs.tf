output "jump_host_public_ip" {
  description = "SSH here; the other VMs are reached through it (ssh -J)."
  value       = azurerm_public_ip.jump.ip_address
}

output "private_ips" {
  description = "Private addresses inside the subnet: VM 0 is the broker and generator, the rest run processors."
  value       = [for n in azurerm_network_interface.vm : n.private_ip_address]
}

output "resource_group" {
  value = azurerm_resource_group.bench.name
}
