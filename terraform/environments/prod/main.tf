# =============================================================================
# ENVIRONMENT: prod
# PURPOSE: Production configuration — HA databases, bigger node pools,
#          longer log retention, geo-replicated ACR, and zone-redundant Redis.
#          Cost estimate: ~$800-1,500/month (real prod costs!)
#
# KEY DIFFERENCES FROM DEV:
#   - PostgreSQL: GeneralPurpose SKU + ZoneRedundant HA
#   - Redis: Standard tier with replicated data
#   - AKS: More nodes, bigger VMs, higher min/max
#   - ACR: Standard SKU with geo-replication
#   - Log retention: 90 days
# =============================================================================

terraform {
  required_version = ">= 1.7.0"
  required_providers {
    azurerm = { source = "hashicorp/azurerm"; version = "~> 3.100" }
    random  = { source = "hashicorp/random";  version = "~> 3.6" }
  }
  backend "azurerm" {
    resource_group_name  = "rg-terraform-state"
    storage_account_name = "stterraformstate"
    container_name       = "tfstate"
    key                  = "microservices/prod/terraform.tfstate"   # Different key!
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = true  # Protect prod!
    }
  }
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${var.project_name}-${var.environment}"
  location = var.location
  tags     = local.common_tags
}

locals {
  common_tags = merge(var.tags, {
    Environment = var.environment
    Project     = var.project_name
    ManagedBy   = "Terraform"
  })
}

module "monitoring" {
  source              = "../../modules/monitoring"
  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  log_retention_days  = 90   # 3x more than dev
  tags                = local.common_tags
}

module "acr" {
  source              = "../../modules/acr"
  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  acr_sku             = "Standard"
  geo_replication_locations = ["westus2"]  # Images replicated to west coast!
  tags                = local.common_tags
}

module "networking" {
  source              = "../../modules/networking"
  project_name        = var.project_name
  environment         = var.environment
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  vnet_address_space  = "10.1.0.0/16"   # Different CIDR from dev to avoid overlap
  aks_subnet_cidr     = "10.1.0.0/22"
  db_subnet_cidr      = "10.1.8.0/24"
  appgw_subnet_cidr   = "10.1.9.0/24"
  tags                = local.common_tags
}

module "keyvault" {
  source                  = "../../modules/keyvault"
  project_name            = var.project_name
  environment             = var.environment
  location                = var.location
  resource_group_name     = azurerm_resource_group.main.name
  aks_kubelet_identity_id = module.aks.kubelet_identity_id
  allowed_subnet_ids      = [module.networking.aks_subnet_id]
  tags                    = local.common_tags
  depends_on              = [module.aks]
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

  # Prod: 3 system nodes spread across 3 availability zones for HA
  system_vm_size    = "Standard_D4s_v3"
  system_node_min   = 3
  system_node_max   = 5
  system_node_count = 3

  # App nodes: larger VMs, more capacity
  app_vm_size    = "Standard_D8s_v3"
  app_node_min   = 3
  app_node_max   = 10
  app_node_count = 3

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
  db_sku_name          = "GP_Standard_D4s_v3"  # GeneralPurpose, 4 vCores
  enable_ha            = true                   # Zone-redundant HA!
  backup_retention_days = 35                    # 5 weeks of backups
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
  redis_sku           = "Standard"  # Standard = replicated (1 primary + 1 replica)
  redis_family        = "C"
  redis_capacity      = 1           # 1GB
  enable_rdb_backup   = true
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
