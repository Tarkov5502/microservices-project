# =============================================================================
# MODULE: aks
# PURPOSE: Provisions the Azure Kubernetes Service cluster — the heart of this
#          platform. AKS manages the Kubernetes control plane for you (free!)
#          and you pay only for the worker nodes (VMs).
#
# WHAT YOU'LL LEARN:
#   - AKS node pools (system vs user pools)
#   - Kubernetes RBAC integration with Azure AD
#   - Managed identities (no passwords for Azure resources!)
#   - Azure CNI networking (pods get real VNet IPs)
#   - Auto-scaling at the node level (Cluster Autoscaler)
#   - Container Insights for monitoring
# =============================================================================

# ─── User-Assigned Managed Identity ──────────────────────────────────────────
# Instead of storing passwords, Azure resources authenticate using Managed
# Identities. AKS uses this identity to create load balancers, pull from ACR,
# write to Log Analytics, etc. — all without any secrets!
resource "azurerm_user_assigned_identity" "aks" {
  name                = "id-aks-${var.project_name}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

# Grant AKS identity the ability to manage resources in the node resource group
# (AKS creates a second RG for VMs/disks/NICs automatically)
resource "azurerm_role_assignment" "aks_network_contributor" {
  scope                = var.vnet_id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_user_assigned_identity.aks.principal_id
}

# ─── AKS Cluster ─────────────────────────────────────────────────────────────
resource "azurerm_kubernetes_cluster" "main" {
  name                = "aks-${var.project_name}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name

  # The DNS prefix appears in your cluster's public FQDN
  dns_prefix = "${var.project_name}-${var.environment}"

  # Kubernetes version — always pin to a specific version in production!
  kubernetes_version = var.kubernetes_version

  # Where AKS stores VM/disk/NIC resources it creates on your behalf
  node_resource_group = "rg-${var.project_name}-${var.environment}-nodes"

  # ─── System Node Pool ─────────────────────────────────────────────────────
  # The system pool runs Kubernetes system components (CoreDNS, kube-proxy,
  # metrics-server). It MUST always exist and should use reliable VM sizes.
  default_node_pool {
    name                = "system"
    node_count          = var.system_node_count
    vm_size             = var.system_vm_size
    vnet_subnet_id      = var.aks_subnet_id
    os_disk_size_gb     = 50
    type                = "VirtualMachineScaleSets"

    # Cluster Autoscaler: AKS will add/remove VMs based on pending pods
    enable_auto_scaling = true
    min_count           = var.system_node_min
    max_count           = var.system_node_max

    # Taint system pool so your app pods don't land here
    only_critical_addons_enabled = true

    node_labels = {
      "nodepool-type" = "system"
      "environment"   = var.environment
    }
  }

  # ─── Identity ─────────────────────────────────────────────────────────────
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aks.id]
  }

  # ─── Networking ───────────────────────────────────────────────────────────
  network_profile {
    # Azure CNI: each pod gets a real IP from the VNet subnet.
    # Alternative is "kubenet" where pods get NAT'd IPs — Azure CNI is better
    # for production because pods are directly addressable.
    network_plugin = "azure"
    network_policy = "azure"       # Enforces Kubernetes NetworkPolicy objects
    load_balancer_sku = "standard" # Standard SKU required for production
    outbound_type     = "loadBalancer"

    # IP ranges reserved for Kubernetes internal services — must NOT overlap VNet
    service_cidr       = "172.16.0.0/16"
    dns_service_ip     = "172.16.0.10"
  }

  # ─── Azure AD RBAC Integration ────────────────────────────────────────────
  # Instead of managing kubeconfig credentials manually, use Azure AD groups
  # to control who can do what in the cluster.
  azure_active_directory_role_based_access_control {
    managed            = true
    azure_rbac_enabled = true
  }

  # ─── Monitoring ───────────────────────────────────────────────────────────
  # Container Insights sends pod logs and metrics to Log Analytics.
  oms_agent {
    log_analytics_workspace_id = var.log_analytics_workspace_id
  }

  # ─── Add-ons ──────────────────────────────────────────────────────────────
  # Key Vault integration: pods can mount Key Vault secrets as files/env vars
  key_vault_secrets_provider {
    secret_rotation_enabled = true
  }

  # ─── OIDC Issuer ──────────────────────────────────────────────────────────
  # Expose an OIDC issuer endpoint so Azure AD can validate tokens that pods
  # present during the Workload Identity token exchange flow.
  # Without this, federated credentials cannot be created or used.
  oidc_issuer_enabled = true

  # ─── Workload Identity ────────────────────────────────────────────────────
  # Installs the mutating webhook that intercepts pod creation. For any pod
  # with the azure.workload.identity/use: "true" label, the webhook:
  #   1. Injects AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE
  #   2. Mounts a projected volume at AZURE_FEDERATED_TOKEN_FILE path
  # Azure SDKs' DefaultAzureCredential detects these vars automatically.
  workload_identity_enabled = true

  tags = var.tags

  lifecycle {
    # Note: prevent_destroy MUST be a literal — Terraform refuses to interpolate
    # variables here. To keep dev tear-downable while still protecting prod, we
    # move the protection to an Azure-side management lock below, guarded by
    # var.enable_destroy_protection.
    ignore_changes = [
      default_node_pool[0].node_count, # Autoscaler manages this
    ]
  }
}

# ─── Destroy Protection (Azure-side lock) ────────────────────────────────────
# CanNotDelete: delete operations against the cluster — whether from Terraform,
# the Azure CLI, or the portal — fail until the lock is removed. Terraform
# applies and updates still work. Set var.enable_destroy_protection=false in
# dev to skip this lock entirely and allow normal teardown.
resource "azurerm_management_lock" "aks_no_delete" {
  count      = var.enable_destroy_protection ? 1 : 0
  name       = "lock-aks-${var.project_name}-${var.environment}"
  scope      = azurerm_kubernetes_cluster.main.id
  lock_level = "CanNotDelete"
  notes      = "Destroy protection. Set enable_destroy_protection=false in tfvars to remove."
}

# ─── User Node Pool ───────────────────────────────────────────────────────────
# Application workloads run here, separate from system components.
# This allows independent scaling of app nodes without touching system nodes.
resource "azurerm_kubernetes_cluster_node_pool" "app" {
  name                  = "app"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.main.id
  vm_size               = var.app_vm_size
  vnet_subnet_id        = var.aks_subnet_id
  os_disk_size_gb       = 50
  mode                  = "User"

  enable_auto_scaling = true
  min_count           = var.app_node_min
  max_count           = var.app_node_max
  node_count          = var.app_node_count

  node_labels = {
    "nodepool-type" = "app"
    "environment"   = var.environment
  }

  # Taint is a "keep away" signal. Combined with a Toleration in pods,
  # this ensures ONLY app pods land on this node pool.
  node_taints = ["workload=app:NoSchedule"]

  tags = var.tags
}

# ─── ACR Pull Permission ──────────────────────────────────────────────────────
# Grant the AKS kubelet identity permission to pull images from our ACR.
# Without this, pods would fail to start because they can't download images!
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                = var.acr_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
}
