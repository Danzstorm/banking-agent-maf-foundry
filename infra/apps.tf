resource "azurerm_container_registry" "main" {
  name                = "acrbankingagent${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_role_assignment" "acr_pull_mcp" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.mcp_server.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-banking-agent-${local.suffix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  logs_destination           = "log-analytics"
  tags                       = local.tags
}

locals {
  placeholder_image = var.mcp_image == "mcr.microsoft.com/k8se/quickstart:latest"
  sql_fqdn          = azurerm_mssql_server.main.fully_qualified_domain_name
  sql_db_name       = azapi_resource.sql_db.name

  # SQLAlchemy URL for mssql-python with the user-assigned identity (no password).
  # ponytail: placeholder shape; the Python side will settle the exact dialect/params when it targets Azure SQL.
  database_url = "mssql+mssqlpython://@${local.sql_fqdn}:1433/${local.sql_db_name}?Authentication=ActiveDirectoryMSI&UID=${azurerm_user_assigned_identity.mcp_server.client_id}&Encrypt=yes"
}

resource "azurerm_container_app" "mcp_server" {
  name                         = "mcp-server"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.mcp_server.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.mcp_server.id
  }

  secret {
    name  = "appinsights-connection-string"
    value = azurerm_application_insights.main.connection_string
  }

  ingress {
    external_enabled = true
    # The quickstart placeholder listens on 80; our server listens on 8000.
    target_port = local.placeholder_image ? 80 : 8000
    transport   = "auto"

    # Phase 5 validates Entra JWTs in code; the IP allowlist stays as defense in depth while
    # the agent host runs on the developer's machine (no public client should reach MCP).
    ip_security_restriction {
      name             = "allow-client-ip"
      action           = "Allow"
      ip_address_range = "${var.client_ip}/32"
    }

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = "mcp-server"
      image  = var.mcp_image
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "AUTH_MODE"
        value = "entra"
      }
      env {
        name  = "ENTRA_TENANT_ID"
        value = var.external_tenant_id
      }
      env {
        name  = "ENTRA_ISSUER"
        value = local.ciam_issuer
      }
      env {
        name  = "ENTRA_AUDIENCE" # v2 access tokens carry the API client id as aud
        value = azuread_application.mcp_api.client_id
      }
      env {
        name  = "ENTRA_JWKS_URI"
        value = local.ciam_jwks_uri
      }
      env {
        name  = "MCP_HOST" # server defaults to 127.0.0.1, unreachable from ingress
        value = "0.0.0.0"
      }
      env {
        name  = "DATABASE_URL"
        value = local.database_url
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.mcp_server.client_id
      }
      env {
        name        = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        secret_name = "appinsights-connection-string"
      }
    }
  }

  # The registry identity needs AcrPull before the first revision pulls from ACR.
  depends_on = [azurerm_role_assignment.acr_pull_mcp]
}
