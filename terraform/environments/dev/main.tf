# =============================================================================
# ENVIRONMENT: dev
# PURPOSE: Wires all Terraform modules together for the development environment.
#          Dev uses smaller/cheaper SKUs. Cost estimate: ~$150-250/month.
#
# TERRAFORM CONCEPT — Root Modules vs Child Modules:
#   This file IS the "root module" — the entry point Terraform runs.
#   It calls "child modules" (our modules/ directory) and passes values in.
#   Think of it as main() calling library functions.
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

  # TERRAFORM CONCEPT — Remote Backend:
  # State lives in Azure Blob Storage with built-in locking (only one apply
  # at a time per state file). Bootstrap the backend ONCE per subscription
  # before this module's first `terraform init` — see terraform/bootstrap/.
  #
  # storage_account_name is a placeholder. Replace it with the value of
  # `terraform output storage_account_name` from the bootstrap module, or
  # override it at init time without editing this file:
  #
  #   terraform init -backend-config="storage_account_name=sttfabc123"
  backend "azurerm" {
    resource_group_name  = "rg-terraform-state"
    storage_account_name = "REPLACE_WITH_BOOTSTRAP_OUTPUT"
    container_name       = "tfstate"
    key                  = "microservices/dev/terraform.tfstate"
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy               = false
      recover_soft_deleted_key_vaults            = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
}

# ─── Resource Group ───────────────────────────────────────────────────────────
# Everything in Azure must belong to a Resource Group. It's a logical container
# for related resources — think of it as a folder.
resource "azurerm_resource_group" "main" {
  name     = "rg-${var.project_name}-${var.environment}"
  location = var.location
  tags     = local.common_tags
}

# ─── Local Values ─────────────────────────────────────────────────────────────
# TERRAFORM CONCEPT — Locals:
# Computed values derived from variables. Used to avoid repetition (DRY!).
locals {
  # NOTE: Do NOT use timestamp() here — it is non-deterministic and causes
  # a perpetual diff on every terraform plan/apply, making Terraform think
  # resources need to be updated every single run.
  common_tags = merge(var.tags, {
    Environment = var.environment
    Project     = var.project_name
    ManagedBy   = "Terraform"
  })
}

# ─── Module Calls ─────────────────────────────────────────────────────────────
# Each module block instantiates a child module. source points to the module
# directory. All other arguments are the module's input variables.

module "monitoring" {
  source              = "../../modules/monitoring"
  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  log_retention_days  = 30
  tags                = local.common_tags
}

module "acr" {
  source              = "../../modules/acr"
  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  acr_sku             = "Basic"   # Basic is fine for dev, no geo-replication needed
  tags                = local.common_tags
}

module "networking" {
  source              = "../../modules/networking"
  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name

  # Note how we pass outputs from one module to another!
  # This is the Terraform data flow pattern.
  vnet_address_space  = "10.0.0.0/16"
  aks_subnet_cidr     = "10.0.0.0/22"
  db_subnet_cidr      = "10.0.8.0/24"
  appgw_subnet_cidr   = "10.0.9.0/24"
  tags                = local.common_tags
}

module "keyvault" {
  source                   = "../../modules/keyvault"
  project_name             = var.project_name
  environment              = var.environment
  location                 = var.location
  resource_group_name      = azurerm_resource_group.main.name
  aks_kubelet_identity_id  = module.aks.kubelet_identity_id
  allowed_subnet_ids       = [module.networking.aks_subnet_id]
  tags                     = local.common_tags

  # AKS must exist before Key Vault RBAC can reference the kubelet identity
  depends_on = [module.aks]
}

module "workload_identity" {
  source              = "../../modules/workload-identity"
  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  oidc_issuer_url     = module.aks.oidc_issuer_url
  key_vault_id        = module.keyvault.key_vault_id
  tags                = local.common_tags

  depends_on = [module.aks, module.keyvault]
}

module "aks" {
  source                     = "../../modules/aks"
  project_name               = var.project_name
  environment                = var.environment
  location                   = var.location
  resource_group_name        = azurerm_resource_group.main.name
  vnet_id                    = module.networking.vnet_id
  aks_subnet_id              = module.networking.aks_subnet_id
  acr_id                     = module.acr.acr_id
  log_analytics_workspace_id = module.monitoring.log_analytics_workspace_id
  kubernetes_version         = var.kubernetes_version

  # Dev: single node, smallest VM (cheaper!)
  system_vm_size   = "Standard_D2s_v3"
  system_node_min  = 1
  system_node_max  = 2
  system_node_count = 1

  app_vm_size      = "Standard_D2s_v3"
  app_node_min     = 1
  app_node_max     = 3
  app_node_count   = 1

  # Dev environments must be tear-downable. Disable the Azure management lock
  # so `terraform destroy` cleans up without manual lock removal.
  enable_destroy_protection = false

  tags = local.common_tags
}

module "database" {
  source               = "../../modules/database"
  project_name         = var.project_name
  environment          = var.environment
  location             = var.location
  resource_group_name  = azurerm_resource_group.main.name
  database_subnet_id   = module.networking.database_subnet_id
  postgres_dns_zone_id = module.networking.postgres_dns_zone_id
  key_vault_id         = module.keyvault.key_vault_id
  db_sku_name          = "B_Standard_B1ms"  # Burstable, cheapest for dev
  enable_ha            = false              # No HA in dev (saves cost)
  tags                 = local.common_tags
}

module "redis" {
  source              = "../../modules/redis"
  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  aks_subnet_id       = module.networking.aks_subnet_id
  key_vault_id        = module.keyvault.key_vault_id
  redis_sku           = "Basic"    # Basic for dev
  redis_family        = "C"
  redis_capacity      = 0          # 250MB — enough for dev
  enable_rdb_backup   = false
  tags                = local.common_tags
}

module "servicebus" {
  source              = "../../modules/servicebus"
  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  key_vault_id        = module.keyvault.key_vault_id
  servicebus_sku      = "Standard"
  tags                = local.common_tags
}
