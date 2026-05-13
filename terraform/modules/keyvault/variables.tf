variable "project_name"             { type = string }
variable "environment"              { type = string }
variable "location"                 { type = string; default = "eastus2" }
variable "resource_group_name"      { type = string }
variable "aks_kubelet_identity_id"  { type = string }
variable "allowed_subnet_ids"       { type = list(string); default = [] }
variable "tags"                     { type = map(string); default = {} }
