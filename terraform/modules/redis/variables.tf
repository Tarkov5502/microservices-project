variable "project_name"         { type = string }
variable "environment"          { type = string }
variable "location"             { type = string; default = "eastus2" }
variable "resource_group_name"  { type = string }
variable "aks_subnet_id"        { type = string }
variable "key_vault_id"         { type = string }
variable "redis_capacity"       { type = number; default = 0 }  # 0=250MB, 1=1GB, 2=2.5GB
variable "redis_family"         { type = string; default = "C" } # C=Basic/Standard, P=Premium
variable "redis_sku"            { type = string; default = "Basic" }
variable "enable_rdb_backup"    { type = bool;   default = false }
variable "tags"                 { type = map(string); default = {} }
