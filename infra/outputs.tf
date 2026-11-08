output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "foundry_endpoint" {
  value = azurerm_cognitive_account.foundry.endpoint
}

output "foundry_project_endpoint" {
  description = "Endpoint for FoundryChatClient / azure-ai-projects."
  value       = local.foundry_project_endpoint
}

output "agent_host_url" {
  value = "https://${azurerm_container_app.agent_host.ingress[0].fqdn}"
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

# --- Phase 5: identity ---------------------------------------------------------

output "entra_authority" {
  value = local.ciam_authority
}

output "entra_tenant_id" {
  value = var.external_tenant_id
}

output "cli_client_id" {
  value = azuread_application.cli.client_id
}

output "agent_api_client_id" {
  value = azuread_application.agent_api.client_id
}

output "mcp_api_client_id" {
  value = azuread_application.mcp_api.client_id
}

output "key_vault_uri" {
  value = azurerm_key_vault.main.vault_uri
}

output "obo_certificate_name" {
  value = azurerm_key_vault_certificate.obo.name
}

# --- Phase 7: A2A -----------------------------------------------------------------

output "productos_a2a_url" {
  description = "Internal URL of the Productos A2A server (card at .well-known/agent-card.json)."
  value       = "https://${azurerm_container_app.productos_a2a.ingress[0].fqdn}/"
}

output "productos_a2a_client_id" {
  description = "Audience of the service token (api://<client id>/.default)."
  value       = azuread_application.productos_a2a.client_id
}

output "github_evals_client_id" {
  description = "Client id of the GitHub Actions app (repo variable AZURE_CLIENT_ID)."
  value       = azuread_application.github_evals.client_id
}
