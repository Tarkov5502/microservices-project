# =============================================================================
# MODULE: workload-identity
# PURPOSE: Create one User-Assigned Managed Identity per microservice, bind
#          each identity to its Kubernetes ServiceAccount via Federated Identity
#          Credential (OIDC), and grant each identity access to only the Key
#          Vault secrets it needs.
#
# WHAT YOU'LL LEARN:
#   - Workload Identity vs Pod Identity (legacy): no mutating webhook needed
#   - OIDC token exchange: K8s ServiceAccount token → Azure AD access token
#   - Federated Identity Credentials: the bridge between K8s and Azure AD
#   - Principle of Least Privilege: each pod gets its own identity with
#     minimal permissions — the api-gateway can't read DB secrets
#
# HOW IT WORKS:
#
#   1. AKS exposes an OIDC issuer URL (e.g. https://oidc.prod-aks.azure.com/<id>/)
#   2. Each pod's ServiceAccount gets a projected volume with a short-lived
#      OIDC token signed by that issuer.
#   3. The Workload Identity webhook injects AZURE_CLIENT_ID + AZURE_FEDERATED_
#      TOKEN_FILE env vars into pods annotated with the right labels.
#   4. Azure SDKs (Key Vault, Service Bus, etc.) use DefaultAzureCredential,
#      which detects these env vars and exchanges the pod token for an Azure
#      AD access token using the OIDC flow.
#   5. The managed identity's RBAC roles determine what that token can do.
#
# RESULT: No secrets in env vars, no secret rotation headaches, full audit
# trail in Azure AD sign-in logs showing exactly which pod authenticated when.
# =============================================================================

variable "project_name"          { type = string }
variable "environment"           { type = string }
variable "location"              { type = string }
variable "resource_group_name"   { type = string }
variable "oidc_issuer_url"       { type = string; description = "AKS OIDC issuer URL from cluster output" }
variable "key_vault_id"          { type = string }
variable "k8s_namespace"         { type = string; default = "microservices" }
variable "tags"                  { type = map(string); default = {} }


# ─── One Managed Identity Per Service ────────────────────────────────────────
# Each service gets its own identity rather than sharing one. Sharing violates
# least privilege: if api-gateway is compromised and uses a shared identity,
# it could read the database password it has no business knowing.

locals {
  services = ["api-gateway", "user-service", "task-service", "notification-service"]
}

resource "azurerm_user_assigned_identity" "services" {
  for_each = toset(local.services)

  name                = "id-${each.key}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}


# ─── Federated Identity Credentials ──────────────────────────────────────────
# This is the OIDC trust binding. It says:
#   "Tokens issued by THIS AKS cluster's OIDC endpoint, for the ServiceAccount
#    named THIS in namespace THIS, are trusted to act as THIS managed identity."
#
# Without this, Azure would reject the pod's OIDC token with "issuer not trusted".

resource "azurerm_federated_identity_credential" "services" {
  for_each = toset(local.services)

  name                = "fic-${each.key}-${var.environment}"
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.services[each.key].id

  # The OIDC issuer URL from the AKS cluster
  issuer = var.oidc_issuer_url

  # Must exactly match the ServiceAccount name and namespace in K8s.
  # Format: system:serviceaccount:<namespace>:<serviceaccount-name>
  subject = "system:serviceaccount:${var.k8s_namespace}:${each.key}"

  # Azure AD audience — must match what the pod's token was issued for.
  # "api://AzureADTokenExchange" is the standard value for Workload Identity.
  audience = ["api://AzureADTokenExchange"]
}


# ─── Key Vault RBAC Assignments ───────────────────────────────────────────────
# Grant each identity ONLY the Key Vault Secrets User role — read-only access.
# They cannot create, update, or delete secrets (that's the Terraform CI role).
#
# We scope each identity to the SAME Key Vault because Key Vault RBAC doesn't
# support per-secret role assignments. To achieve per-secret isolation you'd
# need one Key Vault per service (expensive) or use access policies (deprecated).
# The tradeoff is documented here for the learner.

resource "azurerm_role_assignment" "kv_secrets_user" {
  for_each = toset(local.services)

  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.services[each.key].principal_id
}


# ─── Outputs ─────────────────────────────────────────────────────────────────
output "client_ids" {
  description = "Map of service name → managed identity client ID (inject into K8s ServiceAccount annotations)"
  value = {
    for svc in local.services :
    svc => azurerm_user_assigned_identity.services[svc].client_id
  }
}

output "principal_ids" {
  description = "Map of service name → managed identity principal/object ID"
  value = {
    for svc in local.services :
    svc => azurerm_user_assigned_identity.services[svc].principal_id
  }
}
