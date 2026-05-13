output "server_fqdn"   { value = azurerm_postgresql_flexible_server.main.fqdn }
output "server_name"   { value = azurerm_postgresql_flexible_server.main.name }
output "database_name" { value = azurerm_postgresql_flexible_server_database.app.name }
output "admin_username" { value = azurerm_postgresql_flexible_server.main.administrator_login }
