variable "project_name"          { type = string }
variable "environment"           { type = string }
variable "location"              { type = string; default = "eastus2" }
variable "resource_group_name"   { type = string }
variable "database_subnet_id"    { type = string }
variable "postgres_dns_zone_id"  { type = string }
variable "key_vault_id"          { type = string }
variable "db_admin_username"     { type = string; default = "pgadmin" }
variable "db_name"               { type = string; default = "appdb" }
variable "db_sku_name"           { type = string; default = "B_Standard_B1ms" }
variable "db_storage_mb"         { type = number; default = 32768 }
variable "backup_retention_days" { type = number; default = 7 }
variable "enable_ha"             { type = bool;   default = false }
variable "tags"                  { type = map(string); default = {} }
