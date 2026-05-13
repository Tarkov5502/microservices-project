output "log_analytics_workspace_id"       { value = azurerm_log_analytics_workspace.main.id }
output "log_analytics_workspace_name"     { value = azurerm_log_analytics_workspace.main.name }
output "log_analytics_primary_shared_key" { value = azurerm_log_analytics_workspace.main.primary_shared_key; sensitive = true }
