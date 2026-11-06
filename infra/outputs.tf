output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "foundry_endpoint" {
  value = azurerm_cognitive_account.foundry.endpoint
}

output "foundry_project_endpoint" {
  description = "Endpoint for FoundryChatClient / azure-ai-projects."
  value = lookup(
    azurerm_cognitive_account_project.main.endpoints,
    "AI Foundry API",
    "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.services.ai.azure.com/api/projects/${azurerm_cognitive_account_project.main.name}"
  )
}

output "model_deployment_name" {
  value = azurerm_cognitive_deployment.gpt5mini.name
}

output "sql_server_fqdn" {
  value = local.sql_fqdn
}

output "sql_database_name" {
  value = local.sql_db_name
}

output "acr_login_server" {
  value = azurerm_container_registry.main.login_server
}

output "acr_name" {
  value = azurerm_container_registry.main.name
}

output "mcp_url" {
  description = "Streamable HTTP endpoint of the MCP server (path depends on the server; /mcp by default)."
  value       = "https://${azurerm_container_app.mcp_server.ingress[0].fqdn}/mcp"
}

output "appinsights_connection_string" {
  value     = azurerm_application_insights.main.connection_string
  sensitive = true
}

output "mcp_server_identity_client_id" {
  value = azurerm_user_assigned_identity.mcp_server.client_id
}

output "agent_host_identity_client_id" {
  value = azurerm_user_assigned_identity.agent_host.client_id
}

output "search_endpoint" {
  value = "https://${azurerm_search_service.main.name}.search.windows.net"
}

output "storage_account_name" {
  value = azurerm_storage_account.knowledge.name
}

output "storage_account_id" {
  value = azurerm_storage_account.knowledge.id
}

output "knowledge_container" {
  value = azurerm_storage_container.knowledge.name
}

output "embedding_deployment_name" {
  value = azurerm_cognitive_deployment.embedding.name
}

output "foundry_openai_endpoint" {
  description = "Azure OpenAI-style endpoint of the Foundry account, used by the knowledge source embedding model."
  value       = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.openai.azure.com"
}
