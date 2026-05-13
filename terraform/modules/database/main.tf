# =============================================================================
# MODULE: database (Azure Database for PostgreSQL Flexible Server)
# PURPOSE: Managed PostgreSQL that handles backups, patching, HA, and scaling
#          for you. Lives inside the VNet so it's never exposed to the internet.
#
# WHAT YOU'LL LEARN:
#   - PaaS vs IaaS databases
#   - Private networking for databases (VNet injection)
#   - High Availability with zone-redundant standby
#   - Point-in-time restore (automated backups)
#   - Connection pooling concepts
# =============================================================================

# Random password — Terraform generates it, stores it in state, and we push
# it to Key Vault so apps can fetch it at runtime without hardcoding.
resource "random_password" "postgres" {
  length           = 24
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "azurerm_postgresql_flexible_server" "main" {
  name                   = "psql-${var.project_name}-${var.environment}"
  resource_group_name    = var.resource_group_name
  location               = var.location
  version                = "15"    # PostgreSQL 15 — long-term support

  # Administrator credentials — password is randomly generated above
  administrator_login    = var.db_admin_username
  administrator_password = random_password.postgres.result

  # Private networking: the server is injected INTO your VNet subnet.
  # It has NO public endpoint — only AKS pods inside the VNet can reach it.
  delegated_subnet_id    = var.database_subnet_id
  private_dns_zone_id    = var.postgres_dns_zone_id

  # SKU format: "tier_family_vcores"
  #   Burstable: dev/test (cheap, shares compute)
  #   GeneralPurpose: production workloads
  #   MemoryOptimized: high-memory workloads
  sku_name               = var.db_sku_name

  storage_mb             = var.db_storage_mb

  # Backup configuration — Azure keeps automated backups for this many days
  backup_retention_days  = var.backup_retention_days

  # High Availability: Azure keeps a hot standby in a different availability zone.
  # On failure, it automatically promotes the standby (RPO ~0, RTO ~30s).
  dynamic "high_availability" {
    for_each = var.enable_ha ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = "2"
    }
  }

  # Maintenance window: when Azure can apply patches (choose off-peak hours)
  maintenance_window {
    day_of_week  = 0  # Sunday
    start_hour   = 2  # 2 AM
    start_minute = 0
  }

  tags = var.tags

  lifecycle {
    ignore_changes = [
      zone,                        # Azure may change this after creation
      high_availability[0].standby_availability_zone,
    ]
  }
}

# Create the application database (PostgreSQL server ≠ database)
# Think of the server as the engine, and the database as one schema within it.
resource "azurerm_postgresql_flexible_server_database" "app" {
  name      = var.db_name
  server_id = azurerm_postgresql_flexible_server.main.id
  collation = "en_US.utf8"
  charset   = "UTF8"
}

# Store the connection string in Key Vault so apps never see the raw password
resource "azurerm_key_vault_secret" "db_password" {
  name         = "db-password"
  value        = random_password.postgres.result
  key_vault_id = var.key_vault_id
  tags         = var.tags
}

resource "azurerm_key_vault_secret" "db_connection_string" {
  name  = "db-connection-string"
  value = "postgresql://${var.db_admin_username}:${random_password.postgres.result}@${azurerm_postgresql_flexible_server.main.fqdn}:5432/${var.db_name}"
  key_vault_id = var.key_vault_id
  tags         = var.tags
}
