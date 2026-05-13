variable "project_name"              { type = string }
variable "environment"               { type = string }
variable "location"                  { type = string; default = "eastus2" }
variable "resource_group_name"       { type = string }
variable "vnet_id"                   { type = string }
variable "aks_subnet_id"             { type = string }
variable "acr_id"                    { type = string }
variable "log_analytics_workspace_id" { type = string }
variable "kubernetes_version"        { type = string; default = "1.29" }

variable "system_vm_size"   { type = string; default = "Standard_D2s_v3" }
variable "system_node_count" { type = number; default = 1 }
variable "system_node_min"  { type = number; default = 1 }
variable "system_node_max"  { type = number; default = 3 }

variable "app_vm_size"      { type = string; default = "Standard_D4s_v3" }
variable "app_node_count"   { type = number; default = 1 }
variable "app_node_min"     { type = number; default = 1 }
variable "app_node_max"     { type = number; default = 5 }

variable "tags"             { type = map(string); default = {} }
