variable "project_name"        { type = string }
variable "environment"         { type = string }
variable "location"            { type = string; default = "eastus2" }
variable "resource_group_name" { type = string }
variable "log_retention_days"  { type = number; default = 30 }
variable "action_group_ids"    { type = list(string); default = [] }
variable "tags"                { type = map(string); default = {} }
