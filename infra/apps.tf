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
  sql_url_prefix = "mssql+mssqlpython://@${local.sql_fqdn}:1433/${local.sql_db_name}?Authentication=ActiveDirectoryMSI&Encrypt=yes&UID="
  database_url   = "${local.sql_url_prefix}${azurerm_user_assigned_identity.mcp_server.client_id}"

  # Built from the environment domain, not from the app, so the MCP allowlist can reference the
  # agent host outbound IPs without a dependency cycle.
  mcp_fqdn          = "mcp-server.${azurerm_container_app_environment.main.default_domain}"
  agent_placeholder = var.agent_image == "mcr.microsoft.com/k8se/quickstart:latest"
  foundry_project_endpoint = lookup(
    azurerm_cognitive_account_project.main.endpoints,
    "AI Foundry API",
    "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.services.ai.azure.com/api/projects/${azurerm_cognitive_account_project.main.name}"
  )
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

    # Phase 6: the agent host runs in Container Apps and calls MCP through its public FQDN.
    # ponytail: consumption environments share outbound IPs that can change on platform
    # maintenance; a VNet with a NAT gateway gives a fixed egress IP if this ever matters.
    dynamic "ip_security_restriction" {
      for_each = toset(azurerm_container_app.agent_host.outbound_ip_addresses)
      content {
        name             = "allow-agent-host-${replace(ip_security_restriction.value, ".", "-")}"
        action           = "Allow"
        ip_address_range = "${ip_security_restriction.value}/32"
      }
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

# --- Phase 6: agent host -------------------------------------------------------

resource "azurerm_role_assignment" "acr_pull_agent_host" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.agent_host.principal_id
  principal_type       = "ServicePrincipal"
}

# Same image as mcp-server, other command. Stateless: conversations live in `sesiones`
# (ADR-013), so any replica serves any turn and scale to zero loses nothing.
resource "azurerm_container_app" "agent_host" {
  name                         = "agent-host"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.agent_host.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.agent_host.id
  }

  secret {
    name  = "appinsights-connection-string"
    value = azurerm_application_insights.main.connection_string
  }

  # Public: customers reach it with an Entra token (validated in code), rate limited per user.
  ingress {
    external_enabled = true
    target_port      = local.agent_placeholder ? 80 : 8080
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 2

    http_scale_rule {
      name                = "http"
      concurrent_requests = "10"
    }

    container {
      name    = "agent-host"
      image   = var.agent_image
      command = local.agent_placeholder ? null : ["python", "-m", "agent_host.service"]
      cpu     = 0.5
      memory  = "1Gi"

      dynamic "liveness_probe" {
        for_each = local.agent_placeholder ? [] : [1]
        content {
          transport        = "HTTP"
          port             = 8080
          path             = "/healthz"
          interval_seconds = 10
        }
      }

      dynamic "readiness_probe" {
        for_each = local.agent_placeholder ? [] : [1]
        content {
          transport        = "HTTP"
          port             = 8080
          path             = "/readyz"
          interval_seconds = 5
        }
      }

      dynamic "env" {
        for_each = {
          AUTH_MODE                 = "entra"
          AGENT_BIND                = "0.0.0.0"
          AGENT_PORT                = "8080"
          FOUNDRY_PROJECT_ENDPOINT  = local.foundry_project_endpoint
          FOUNDRY_MODEL_DEPLOYMENT  = azurerm_cognitive_deployment.gpt5mini.name
          MCP_URL                   = "https://${local.mcp_fqdn}/mcp"
          SEARCH_ENDPOINT           = "https://${azurerm_search_service.main.name}.search.windows.net"
          ENTRA_AUTHORITY           = local.ciam_authority
          ENTRA_CLI_CLIENT_ID       = azuread_application.cli.client_id
          ENTRA_AGENT_API_CLIENT_ID = azuread_application.agent_api.client_id
          ENTRA_MCP_API_CLIENT_ID   = azuread_application.mcp_api.client_id
          KEY_VAULT_URI             = azurerm_key_vault.main.vault_uri
          OBO_CERT_NAME             = azurerm_key_vault_certificate.obo.name
          DATABASE_URL              = "${local.sql_url_prefix}${azurerm_user_assigned_identity.agent_host.client_id}"
          AZURE_CLIENT_ID           = azurerm_user_assigned_identity.agent_host.client_id
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name        = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        secret_name = "appinsights-connection-string"
      }
    }
  }

  depends_on = [azurerm_role_assignment.acr_pull_agent_host]
}
