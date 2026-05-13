# =============================================================================
# MODULE: acr (Azure Container Registry)
# PURPOSE: A private Docker registry hosted in Azure. Instead of using Docker
#          Hub (public), your images live here — private, fast, and integrated
#          with AKS via Managed Identity (no passwords!).
#
# WHAT YOU'LL LEARN:
#   - Container registries and image tagging strategy
#   - Azure RBAC for registry access
#   - Geo-replication for multi-region setups
#   - Image vulnerability scanning
# =============================================================================

resource "azurerm_container_registry" "main" {
  name                = "acr${replace(var.project_name, "-", "")}${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location

  # SKU tiers:
  #   Basic   → dev/test, no geo-replication, smaller storage
  #   Standard → production, supports Geo-replication
  #   Premium  → adds Private Link, customer-managed keys, retention policies
  sku = var.acr_sku

  # Admin account is DISABLED — we use Managed Identity instead.
  # The admin account is a shared credential (bad security practice).
  admin_enabled = false

  # Geo-replication pushes your images to multiple Azure regions so pods in
  # any region can pull fast without crossing the globe.
  dynamic "georeplications" {
    for_each = var.geo_replication_locations
    content {
      location                = georeplications.value
      zone_redundancy_enabled = false
      tags                    = var.tags
    }
  }

  tags = var.tags
}
