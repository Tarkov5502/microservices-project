# =============================================================================
# MODULE: servicebus (Azure Service Bus)
# PURPOSE: Enterprise-grade message broker for async communication between
#          microservices. When the task-service creates a task, it sends an
#          event to Service Bus. The notification-service picks it up and sends
#          a notification — without the two services knowing about each other!
#
# WHAT YOU'LL LEARN:
#   - Queues vs Topics vs Subscriptions
#   - Producer/Consumer pattern (decoupled services)
#   - Dead-letter queues (handling failed messages)
#   - Message sessions (ordered processing)
#   - Shared Access Signatures (SAS) for authentication
# =============================================================================

resource "azurerm_servicebus_namespace" "main" {
  name                = "sb-${var.project_name}-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name

  # SKU tiers:
  #   Basic   → Queues only, no Topics, dev/test
  #   Standard → Topics + Subscriptions, most apps
  #   Premium  → Dedicated capacity, VNet integration, larger messages
  sku = var.servicebus_sku

  # Minimum TLS version for client connections
  minimum_tls_version = "1.2"

  tags = var.tags
}

# ─── Topics (Pub/Sub) ────────────────────────────────────────────────────────
# A Topic is like a radio station — multiple subscribers (notification-service,
# analytics-service, audit-service) can all receive the SAME event.

resource "azurerm_servicebus_topic" "task_events" {
  name         = "task-events"
  namespace_id = azurerm_servicebus_namespace.main.id

  # Message TTL: how long a message lives if no consumer picks it up
  default_message_ttl = "P1D"  # ISO 8601 duration: 1 day

  # Enable duplicate detection: if the same message is published twice within
  # the window, Service Bus discards the duplicate.
  requires_duplicate_detection          = true
  duplicate_detection_history_time_window = "PT10M"  # 10 minutes

  # Partitioning: messages are distributed across multiple brokers for throughput
  enable_partitioning = true
}

resource "azurerm_servicebus_topic" "user_events" {
  name         = "user-events"
  namespace_id = azurerm_servicebus_namespace.main.id
  default_message_ttl = "P1D"
  requires_duplicate_detection = true
  duplicate_detection_history_time_window = "PT10M"
  enable_partitioning = true
}

# ─── Subscriptions ───────────────────────────────────────────────────────────
# Each subscriber gets its own copy of messages. Notification-service subscribes
# to task-events and processes them independently.

resource "azurerm_servicebus_subscription" "notifications_task_events" {
  name               = "notification-service"
  topic_id           = azurerm_servicebus_topic.task_events.id
  max_delivery_count = 10  # Retry up to 10 times before dead-lettering

  # Dead-lettering: if processing fails max_delivery_count times, the message
  # moves to the dead-letter sub-queue for manual investigation.
  dead_lettering_on_message_expiration = true
}

resource "azurerm_servicebus_subscription" "notifications_user_events" {
  name               = "notification-service"
  topic_id           = azurerm_servicebus_topic.user_events.id
  max_delivery_count = 10
  dead_lettering_on_message_expiration = true
}

# ─── Authorization Rules ─────────────────────────────────────────────────────
# Least-privilege: each service gets only the permissions it needs.
# task-service can only SEND, notification-service can only LISTEN.

resource "azurerm_servicebus_namespace_authorization_rule" "sender" {
  name         = "task-service-sender"
  namespace_id = azurerm_servicebus_namespace.main.id
  send         = true
  listen       = false
  manage       = false
}

resource "azurerm_servicebus_namespace_authorization_rule" "listener" {
  name         = "notification-service-listener"
  namespace_id = azurerm_servicebus_namespace.main.id
  send         = false
  listen       = true
  manage       = false
}

# Store connection strings in Key Vault
resource "azurerm_key_vault_secret" "sb_sender_connection_string" {
  name         = "sb-sender-connection-string"
  value        = azurerm_servicebus_namespace_authorization_rule.sender.primary_connection_string
  key_vault_id = var.key_vault_id
  tags         = var.tags
}

resource "azurerm_key_vault_secret" "sb_listener_connection_string" {
  name         = "sb-listener-connection-string"
  value        = azurerm_servicebus_namespace_authorization_rule.listener.primary_connection_string
  key_vault_id = var.key_vault_id
  tags         = var.tags
}
