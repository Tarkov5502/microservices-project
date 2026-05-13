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

# When true, an Azure Resource Lock is applied to the cluster that requires
# the lock be removed (manually or via Terraform) before deletion can succeed.
# Prefer this over Terraform's lifecycle.prevent_destroy because:
#   1. prevent_destroy must be a literal — it cannot be driven by a variable,
#      which forced the previous module to use the same value for dev and prod.
#   2. The Azure-side lock survives Terraform state loss; prevent_destroy only
#      protects against `terraform destroy` runs against this exact state file.
# Recommended: true in prod, false in dev/test so you can tear down freely.
variable "enable_destroy_protection" {
  type        = bool
  default     = true
  description = "Apply an azurerm_management_lock(CanNotDelete) to the AKS cluster."
}
