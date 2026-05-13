output "namespace_name"     { value = azurerm_servicebus_namespace.main.name }
output "namespace_id"       { value = azurerm_servicebus_namespace.main.id }
output "task_events_topic"  { value = azurerm_servicebus_topic.task_events.name }
output "user_events_topic"  { value = azurerm_servicebus_topic.user_events.name }
