# =============================================================================
# MODULE: networking — Variables
# PURPOSE: All inputs this module accepts. Callers (environments) pass these in.
#
# TERRAFORM CONCEPT — Variables:
#   Variables make modules reusable. Instead of hardcoding "dev" or "prod",
#   the caller decides what values to inject. This is the same as function
#   parameters in programming.
# =============================================================================

variable "project_name" {
  description = "Short name for the project, used in resource naming (e.g. 'microservices')"
  type        = string
}

variable "environment" {
  description = "Deployment environment: 'dev', 'staging', or 'prod'"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be one of: dev, staging, prod."
  }
}

variable "location" {
  description = "Azure region where resources will be created (e.g. 'eastus2')"
  type        = string
  default     = "eastus2"
}

variable "resource_group_name" {
  description = "Name of the Azure Resource Group that owns these resources"
  type        = string
}

variable "vnet_address_space" {
  description = "CIDR block for the Virtual Network (e.g. '10.0.0.0/16')"
  type        = string
  default     = "10.0.0.0/16"
}

variable "aks_subnet_cidr" {
  description = "CIDR block for the AKS node/pod subnet (e.g. '10.0.0.0/22')"
  type        = string
  default     = "10.0.0.0/22"
}

variable "db_subnet_cidr" {
  description = "CIDR block for the database subnet (e.g. '10.0.8.0/24')"
  type        = string
  default     = "10.0.8.0/24"
}

variable "appgw_subnet_cidr" {
  description = "CIDR block for the Application Gateway subnet (e.g. '10.0.9.0/24')"
  type        = string
  default     = "10.0.9.0/24"
}

variable "tags" {
  description = "Map of tags to apply to all resources (for cost tracking + governance)"
  type        = map(string)
  default     = {}
}
