# =============================================================================
# MODULE: keyvault (Azure Key Vault)
# PURPOSE: Centralized secrets store. Instead of putting passwords in env vars
#          or config files (which end up in git), you store them in Key Vault
#          and apps fetch them at runtime using Managed Identity — no secrets
#          ever touch your code or CI pipeline!
#
# WHAT YOU'LL LEARN:
#   - Secrets vs Keys vs Certificates (Key Vault stores all three)
#   - Access policies vs RBAC (we use RBAC — it's the modern way)
#   - Soft-delete and purge protection (prevent accidental deletion)
#   - AKS Secret Store CSI Driver (mount KV secrets as pod volumes)
# =============================================================================

data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "main" {
  name                = "kv-${var.project_name}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tenant_id           = data.azurerm_client_config.current.tenant_id

  sku_name = "standard"  # "premium" adds HSM-backed keys (Hardware Security Module)

  # Soft-delete: deleted secrets are retained for 90 days (recoverable)
  soft_delete_retention_days = 90

  # Purge protection: even admins can't permanently delete for 90 days.
  # CRITICAL for compliance — prevents ransomware from destroying your secrets.
  purge_protection_enabled = true

  # Use Azure RBAC for data plane access (modern approach vs access policies)
  enable_rbac_authorization = true

  # Network rules: only allow access from within the VNet
  network_acls {
    bypass                     = "AzureServices"  # Allow trusted Azure services
    default_action             = "Deny"           # Block everything else
    virtual_network_subnet_ids = var.allowed_subnet_ids
  }

  tags = var.tags
}

# ─── RBAC Assignments ────────────────────────────────────────────────────────
# Grant Terraform service principal (running in CI/CD) ability to write secrets
resource "azurerm_role_assignment" "terraform_kv_admin" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Administrator"
  principal_id         = data.azurerm_client_config.current.object_id
}

# Grant AKS kubelet identity ability to READ secrets at runtime
resource "azurerm_role_assignment" "aks_kv_reader" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = var.aks_kubelet_identity_id
}
