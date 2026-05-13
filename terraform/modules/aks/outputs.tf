output "cluster_id"              { value = azurerm_kubernetes_cluster.main.id }
output "cluster_name"            { value = azurerm_kubernetes_cluster.main.name }
output "cluster_fqdn"            { value = azurerm_kubernetes_cluster.main.fqdn }
output "kube_config"             { value = azurerm_kubernetes_cluster.main.kube_config_raw; sensitive = true }
output "kubelet_identity_id"     { value = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id }
output "aks_identity_client_id"  { value = azurerm_user_assigned_identity.aks.client_id }
output "node_resource_group"     { value = azurerm_kubernetes_cluster.main.node_resource_group }
# Required by the workload-identity module to bind federated credentials
output "oidc_issuer_url"         { value = azurerm_kubernetes_cluster.main.oidc_issuer_url }
