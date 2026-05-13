# =============================================================================
# MODULE: networking — Outputs
# PURPOSE: Values this module "exports" so other modules can reference them.
#
# TERRAFORM CONCEPT — Outputs:
#   Like return values from a function. The AKS module needs the subnet ID from
#   this networking module — outputs are how that data flows between modules.
# =============================================================================

output "vnet_id" {
  description = "Resource ID of the Virtual Network"
  value       = azurerm_virtual_network.main.id
}

output "vnet_name" {
  description = "Name of the Virtual Network"
  value       = azurerm_virtual_network.main.name
}

output "aks_subnet_id" {
  description = "Resource ID of the AKS node subnet"
  value       = azurerm_subnet.aks.id
}

output "database_subnet_id" {
  description = "Resource ID of the database subnet"
  value       = azurerm_subnet.database.id
}

output "appgw_subnet_id" {
  description = "Resource ID of the Application Gateway subnet"
  value       = azurerm_subnet.appgw.id
}

output "postgres_dns_zone_id" {
  description = "Resource ID of the private DNS zone for PostgreSQL"
  value       = azurerm_private_dns_zone.postgres.id
}

output "postgres_dns_zone_name" {
  description = "Name of the private DNS zone for PostgreSQL"
  value       = azurerm_private_dns_zone.postgres.name
}
