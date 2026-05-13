output "resource_group_name"       { value = azurerm_resource_group.main.name }
output "aks_cluster_name"          { value = module.aks.cluster_name }
output "acr_login_server"          { value = module.acr.acr_login_server }
output "key_vault_name"            { value = module.keyvault.key_vault_name }
output "postgres_server_fqdn"      { value = module.database.server_fqdn }
output "servicebus_namespace"      { value = module.servicebus.namespace_name }
output "log_analytics_workspace"   { value = module.monitoring.log_analytics_workspace_name }
