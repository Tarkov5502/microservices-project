# =============================================================================
# MODULE: redis (Azure Cache for Redis)
# PURPOSE: In-memory key-value store used for:
#   1. Session caching (user auth tokens)
#   2. Response caching (avoid hitting DB for hot data)
#   3. Rate limiting counters
#   4. Pub/Sub messaging (lightweight alternative to Service Bus)
#
# WHAT YOU'LL LEARN:
#   - Cache-aside pattern
#   - Redis data structures (strings, hashes, sorted sets)
#   - Eviction policies (LRU, LFU)
#   - Private endpoints (network isolation for PaaS)
# =============================================================================

resource "azurerm_redis_cache" "main" {
  name                = "redis-${var.project_name}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  capacity            = var.redis_capacity
  family              = var.redis_family
  sku_name            = var.redis_sku

  # Force TLS — never send data to Redis in plaintext!
  enable_non_ssl_port = false
  minimum_tls_version = "1.2"

  redis_configuration {
    # Maximum memory policy: when Redis is full, which keys get evicted?
    # allkeys-lru = Least Recently Used (good general default for caching)
    maxmemory_policy = "allkeys-lru"

    # Persist data to disk (RDB snapshots) — for session data you want this!
    rdb_backup_enabled            = var.enable_rdb_backup
    rdb_backup_frequency          = 60   # minutes
    rdb_backup_max_snapshot_count = 1
  }

  tags = var.tags
}

# Store Redis connection info in Key Vault
resource "azurerm_key_vault_secret" "redis_connection_string" {
  name         = "redis-connection-string"
  value        = azurerm_redis_cache.main.primary_connection_string
  key_vault_id = var.key_vault_id
  tags         = var.tags
}

# Private Endpoint — gives Redis a private IP inside your VNet.
# Without this, Redis would be accessible from the public internet (bad!).
resource "azurerm_private_endpoint" "redis" {
  name                = "pe-redis-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.aks_subnet_id

  private_service_connection {
    name                           = "psc-redis-${var.environment}"
    private_connection_resource_id = azurerm_redis_cache.main.id
    subresource_names              = ["redisCache"]
    is_manual_connection           = false
  }

  tags = var.tags
}
