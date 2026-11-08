# --- Phase 7: Productos as a remote A2A agent (ADR-015) -------------------------
#
# Service-to-service auth stays in the WORKFORCE tenant (default azuread provider): the agent
# host's managed identity gets an app-only token for the `banking-productos-a2a` app with the
# role Productos.Invoke. Customers (External ID) never reach Productos, and Productos needs no
# customer identity, so there is no second OBO chain.

resource "azurerm_user_assigned_identity" "productos_a2a" {
  name                = "id-productos-a2a"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

# Same data-plane roles the agent host uses for the in-process Productos: model + KB reads.
resource "azurerm_role_assignment" "acr_pull_productos_a2a" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.productos_a2a.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "foundry_user_productos_a2a" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Foundry User"
  principal_id         = azurerm_user_assigned_identity.productos_a2a.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "search_data_reader_productos_a2a" {
  scope                = azurerm_search_service.main.id
  role_definition_name = "Search Index Data Reader"
  principal_id         = azurerm_user_assigned_identity.productos_a2a.principal_id
  principal_type       = "ServicePrincipal"
}

# --- Audience of the service token ---------------------------------------------

resource "random_uuid" "productos_invoke_role" {}

resource "azuread_application" "productos_a2a" {
  display_name     = "banking-productos-a2a"
  sign_in_audience = "AzureADMyOrg"

  api {
    requested_access_token_version = 2 # aud = client id, iss = login.microsoftonline.com/<tid>/v2.0
  }

  app_role {
    id                   = random_uuid.productos_invoke_role.result
    value                = "Productos.Invoke"
    allowed_member_types = ["Application"]
    display_name         = "Invocar Productos por A2A"
    description          = "Permite llamar al agente Productos por A2A (solo el agente host)."
    enabled              = true
  }

  lifecycle {
    ignore_changes = [identifier_uris] # managed by azuread_application_identifier_uri
  }
}

resource "azuread_application_identifier_uri" "productos_a2a" {
  application_id = azuread_application.productos_a2a.id
  identifier_uri = "api://${azuread_application.productos_a2a.client_id}"
}

resource "azuread_service_principal" "productos_a2a" {
  client_id = azuread_application.productos_a2a.client_id
  # Entra refuses tokens to identities without an assignment; the server checks the role too.
  app_role_assignment_required = true
}

resource "azuread_app_role_assignment" "agent_host_invokes_productos" {
  app_role_id         = random_uuid.productos_invoke_role.result
  principal_object_id = azurerm_user_assigned_identity.agent_host.principal_id
  resource_object_id  = azuread_service_principal.productos_a2a.object_id
}

# --- Container App --------------------------------------------------------------

locals {
  productos_a2a_port = 8090
  # The card advertises the internal URL; computed from the environment because an app cannot
  # read its own ingress FQDN.
  productos_a2a_url = "https://productos-a2a.internal.${azurerm_container_app_environment.main.default_domain}/"
}

# Same image as the agent host and MCP server, other command.
resource "azurerm_container_app" "productos_a2a" {
  name                         = "productos-a2a"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.productos_a2a.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.productos_a2a.id
  }

  secret {
    name  = "appinsights-connection-string"
    value = azurerm_application_insights.main.connection_string
  }

  # Internal: only apps of this environment (the agent host) reach it. The token check runs
  # anyway on every JSON-RPC call; the card and /healthz are public inside the environment.
  ingress {
    external_enabled = false
    target_port      = local.agent_placeholder ? 80 : local.productos_a2a_port
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name    = "productos-a2a"
      image   = var.agent_image
      command = local.agent_placeholder ? null : ["python", "-m", "agent_host.productos_a2a"]
      cpu     = 0.5
      memory  = "1Gi"

      dynamic "liveness_probe" {
        for_each = local.agent_placeholder ? [] : [1]
        content {
          transport        = "HTTP"
          port             = local.productos_a2a_port
          path             = "/healthz"
          interval_seconds = 10
        }
      }

      dynamic "readiness_probe" {
        for_each = local.agent_placeholder ? [] : [1]
        content {
          transport        = "HTTP"
          port             = local.productos_a2a_port
          path             = "/healthz"
          interval_seconds = 5
        }
      }

      dynamic "env" {
        for_each = {
          A2A_BIND                             = "0.0.0.0"
          A2A_PORT                             = tostring(local.productos_a2a_port)
          A2A_PUBLIC_URL                       = local.productos_a2a_url
          A2A_TENANT_ID                        = data.azurerm_client_config.current.tenant_id
          A2A_AUDIENCE                         = azuread_application.productos_a2a.client_id
          FOUNDRY_PROJECT_ENDPOINT             = local.foundry_project_endpoint
          FOUNDRY_MODEL_DEPLOYMENT             = azurerm_cognitive_deployment.gpt5mini.name
          SEARCH_ENDPOINT                      = "https://${azurerm_search_service.main.name}.search.windows.net"
          AZURE_CLIENT_ID                      = azurerm_user_assigned_identity.productos_a2a.client_id
          OTEL_INSTRUMENTATION_A2A_SDK_ENABLED = "false" # agent_host/telemetry.py
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

  depends_on = [azurerm_role_assignment.acr_pull_productos_a2a]
}
