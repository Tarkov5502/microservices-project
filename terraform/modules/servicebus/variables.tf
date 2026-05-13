variable "project_name"        { type = string }
variable "environment"         { type = string }
variable "location"            { type = string; default = "eastus2" }
variable "resource_group_name" { type = string }
variable "key_vault_id"        { type = string }
variable "servicebus_sku"      { type = string; default = "Standard" }
variable "tags"                { type = map(string); default = {} }
