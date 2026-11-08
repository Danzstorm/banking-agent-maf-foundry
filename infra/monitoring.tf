resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-banking-agent-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 1
  tags                = local.tags
}

resource "azurerm_application_insights" "main" {
  name                = "appi-banking-agent-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "other"
  tags                = local.tags
}

# Foundry tracing: the project's Application Insights connection (portal: Agents > Traces and
# Monitoring). The connection string is the credential, kept out of the plan with sensitive_body.
resource "azapi_resource" "foundry_appinsights" {
  type      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01"
  name      = "appi-banking-agent"
  parent_id = azurerm_cognitive_account_project.main.id
  body = {
    properties = {
      category      = "AppInsights"
      target        = azurerm_application_insights.main.id
      authType      = "ApiKey"
      isSharedToAll = false
      metadata = {
        ApiType    = "Azure"
        ResourceId = azurerm_application_insights.main.id
      }
    }
  }
  sensitive_body = {
    properties = {
      credentials = { key = azurerm_application_insights.main.connection_string }
    }
  }
}
