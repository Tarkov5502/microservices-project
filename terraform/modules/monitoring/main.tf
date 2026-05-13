# =============================================================================
# MODULE: monitoring (Log Analytics + Container Insights)
# PURPOSE: Collect logs and metrics from all Azure resources and AKS pods
#          into a central workspace for querying, alerting, and dashboards.
#
# WHAT YOU'LL LEARN:
#   - Log Analytics workspace (Azure's Elasticsearch-equivalent)
#   - Container Insights (pre-built K8s dashboards in Azure Portal)
#   - KQL (Kusto Query Language) for log analysis
#   - Alert rules and action groups
# =============================================================================

resource "azurerm_log_analytics_workspace" "main" {
  name                = "law-${var.project_name}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name

  # Retention: how many days to keep logs. More days = higher cost.
  # 30 days is usually enough for operational troubleshooting.
  retention_in_days = var.log_retention_days

  # SKU: PerGB2018 is the modern pay-per-ingestion pricing model.
  sku = "PerGB2018"

  tags = var.tags
}

# Container Insights solution adds pre-built dashboards in the Azure Portal
# showing pod resource usage, container logs, node health, etc.
resource "azurerm_log_analytics_solution" "container_insights" {
  solution_name         = "ContainerInsights"
  location              = var.location
  resource_group_name   = var.resource_group_name
  workspace_resource_id = azurerm_log_analytics_workspace.main.id
  workspace_name        = azurerm_log_analytics_workspace.main.name

  plan {
    publisher = "Microsoft"
    product   = "OMSGallery/ContainerInsights"
  }
}

# Alert: notify when a pod is in CrashLoopBackOff for more than 5 minutes
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "pod_crash_alert" {
  name                = "alert-pod-crashloop-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  scopes              = [azurerm_log_analytics_workspace.main.id]
  description         = "Alert when pods are in CrashLoopBackOff"
  enabled             = true

  evaluation_frequency = "PT5M"   # Check every 5 minutes
  window_duration      = "PT5M"   # Look at last 5 minutes of data

  criteria {
    query = <<-QUERY
      KubePodInventory
      | where Namespace == "microservices"
      | where PodStatus == "Failed" or ContainerStatusReason == "CrashLoopBackOff"
      | summarize Count = count() by bin(TimeGenerated, 5m), PodName
      | where Count > 0
    QUERY

    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = var.action_group_ids
  }

  tags = var.tags
}
