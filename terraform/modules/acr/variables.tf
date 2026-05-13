variable "project_name"         { type = string }
variable "environment"          { type = string }
variable "location"             { type = string; default = "eastus2" }
variable "resource_group_name"  { type = string }
variable "acr_sku"              { type = string; default = "Basic" }
variable "geo_replication_locations" { type = list(string); default = [] }
variable "tags"                 { type = map(string); default = {} }
