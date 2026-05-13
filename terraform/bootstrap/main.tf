# =============================================================================
# TERRAFORM REMOTE STATE BOOTSTRAP
#
# This is the chicken-and-egg fix for the azurerm backend used by every other
# environment. The dev and prod root modules declare:
#
#   backend "azurerm" {
#     resource_group_name  = "rg-terraform-state"
#     storage_account_name = "<unique>"
#     container_name       = "tfstate"
#     ...
#   }
#
# Those resources have to exist BEFORE `terraform init` can succeed against
# them. We can't provision them with the same Terraform module that uses them
# as a backend — the init would fail before the apply could run. So this
# module uses a LOCAL backend and creates exactly the three things the real
# environments need.
#
# RUN ONCE PER SUBSCRIPTION (not per environment). The same backend is shared
# by dev and prod via different state-file keys.
#
# USAGE:
#   az login
#   az account set --subscription "<subscription-id>"
#   cd terraform/bootstrap
#   # Pick a globally unique storage account name (3-24 lowercase letters/digits).
#   # Set it as STORAGE_ACCOUNT below or via TF_VAR_storage_account_name.
#   terraform init
#   terraform apply
#   # Now copy the output `storage_account_name` into dev/main.tf and prod/main.tf
#   # under the backend "azurerm" block.
# =============================================================================

terraform {
  required_version = ">= 1.7.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.100"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # LOCAL backend. The whole point: this state file lives on your laptop,
  # not in Azure, so we can bootstrap the Azure backend itself. Commit the
  # .terraform.lock.hcl, not the state file (terraform.tfstate).
}

provider "azurerm" {
  features {}
}

variable "location" {
  type        = string
  default     = "eastus2"
  description = "Azure region for the state storage account."
}

variable "storage_account_name" {
  type        = string
  default     = ""
  description = <<-EOT
    Globally unique storage account name (3-24 lowercase letters and digits).
    Storage account names share a global Azure namespace, so "stterraformstate"
    is almost certainly taken. If left empty, a random suffix is generated.
  EOT
}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
  numeric = true
}

locals {
  # If the caller didn't supply a name, build one with a random suffix.
  # Pattern: "sttf<6-char-random>" — 10 chars total, well under the 24 cap.
  resolved_storage_account_name = (
    var.storage_account_name != ""
    ? var.storage_account_name
    : "sttf${random_string.suffix.result}"
  )
}

resource "azurerm_resource_group" "tfstate" {
  name     = "rg-terraform-state"
  location = var.location
  tags = {
    Purpose   = "TerraformRemoteState"
    ManagedBy = "Terraform"
  }
}

resource "azurerm_storage_account" "tfstate" {
  name                            = local.resolved_storage_account_name
  resource_group_name             = azurerm_resource_group.tfstate.name
  location                        = azurerm_resource_group.tfstate.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  account_kind                    = "StorageV2"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false

  # State files contain resource IDs, sometimes secrets in older provider
  # versions. Soft-delete + versioning means an accidental `terraform destroy`
  # against the bootstrap is recoverable.
  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  tags = {
    Purpose   = "TerraformRemoteState"
    ManagedBy = "Terraform"
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_name  = azurerm_storage_account.tfstate.name
  container_access_type = "private"
}

# Apply a delete lock so this bootstrap is not destroyed casually. Removing
# the lock requires explicit intent.
resource "azurerm_management_lock" "tfstate_no_delete" {
  name       = "lock-tfstate-no-delete"
  scope      = azurerm_resource_group.tfstate.id
  lock_level = "CanNotDelete"
  notes      = "Protects the Terraform remote state backend."
}

output "storage_account_name" {
  value       = azurerm_storage_account.tfstate.name
  description = "Copy this into the backend \"azurerm\" block of dev/main.tf and prod/main.tf."
}

output "resource_group_name" {
  value = azurerm_resource_group.tfstate.name
}

output "container_name" {
  value = azurerm_storage_container.tfstate.name
}
