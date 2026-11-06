# --- Phase 5: customer identity (Entra External ID) --------------------------
#
# Three app registrations live in the EXTERNAL tenant (provider azuread.external):
#   banking-cli        public client, device code; asks for access_as_user on the agent API.
#   banking-agent-api  middle tier: exposes access_as_user, does OBO to the MCP API with a
#                      certificate (a managed identity cannot federate across tenants, ADR-009).
#   banking-mcp-api    exposes tools.access; issues v2 access tokens validated by the MCP server.
# The certificate is generated inside Key Vault (workforce subscription); only its public part
# reaches Entra. The user flow (sign-in experience) is created by infra/identity/user_flow.py,
# because azuread has no resource for External ID user flows.

locals {
  ciam_authority = "https://${var.external_tenant_subdomain}.ciamlogin.com/${var.external_tenant_id}"
  # Verified against the tenant's OIDC discovery document (v2.0/.well-known/openid-configuration).
  ciam_issuer   = "https://${var.external_tenant_id}.ciamlogin.com/${var.external_tenant_id}/v2.0"
  ciam_jwks_uri = "${local.ciam_authority}/discovery/v2.0/keys"
  graph_scopes  = ["openid", "offline_access"]
}

data "azuread_service_principal" "msgraph" {
  provider  = azuread.external
  client_id = "00000003-0000-0000-c000-000000000000"
}

resource "random_uuid" "mcp_scope" {}
resource "random_uuid" "agent_scope" {}

# --- MCP API -----------------------------------------------------------------

resource "azuread_application" "mcp_api" {
  provider         = azuread.external
  display_name     = "banking-mcp-api"
  sign_in_audience = "AzureADMyOrg"

  api {
    requested_access_token_version = 2

    oauth2_permission_scope {
      id                         = random_uuid.mcp_scope.result
      value                      = "tools.access"
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "Usar las herramientas bancarias"
      admin_consent_description  = "Permite llamar al servidor MCP en nombre del cliente."
      user_consent_display_name  = "Usar las herramientas bancarias"
      user_consent_description   = "Permite operar sobre tus productos en tu nombre."
    }
  }

  lifecycle {
    ignore_changes = [identifier_uris] # managed by azuread_application_identifier_uri
  }
}

resource "azuread_application_identifier_uri" "mcp_api" {
  provider       = azuread.external
  application_id = azuread_application.mcp_api.id
  identifier_uri = "api://${azuread_application.mcp_api.client_id}"
}

resource "azuread_service_principal" "mcp_api" {
  provider  = azuread.external
  client_id = azuread_application.mcp_api.client_id
}

# --- Agent API (middle tier) ---------------------------------------------------

resource "azuread_application" "agent_api" {
  provider         = azuread.external
  display_name     = "banking-agent-api"
  sign_in_audience = "AzureADMyOrg"

  api {
    requested_access_token_version = 2

    oauth2_permission_scope {
      id                         = random_uuid.agent_scope.result
      value                      = "access_as_user"
      type                       = "User"
      enabled                    = true
      admin_consent_display_name = "Conversar con el agente"
      admin_consent_description  = "Permite usar el agente bancario en nombre del cliente."
      user_consent_display_name  = "Conversar con el agente"
      user_consent_description   = "Permite usar el agente bancario en tu nombre."
    }
  }

  required_resource_access {
    resource_app_id = azuread_application.mcp_api.client_id
    resource_access {
      id   = random_uuid.mcp_scope.result
      type = "Scope"
    }
  }

  required_resource_access {
    resource_app_id = data.azuread_service_principal.msgraph.client_id
    dynamic "resource_access" {
      for_each = local.graph_scopes
      content {
        id   = data.azuread_service_principal.msgraph.oauth2_permission_scope_ids[resource_access.value]
        type = "Scope"
      }
    }
  }

  lifecycle {
    ignore_changes = [identifier_uris]
  }
}

resource "azuread_application_identifier_uri" "agent_api" {
  provider       = azuread.external
  application_id = azuread_application.agent_api.id
  identifier_uri = "api://${azuread_application.agent_api.client_id}"
}

resource "azuread_service_principal" "agent_api" {
  provider  = azuread.external
  client_id = azuread_application.agent_api.client_id
}

resource "azuread_application_certificate" "agent_api" {
  provider       = azuread.external
  application_id = azuread_application.agent_api.id
  type           = "AsymmetricX509Cert"
  encoding       = "hex"
  value          = azurerm_key_vault_certificate.obo.certificate_data
  end_date       = azurerm_key_vault_certificate.obo.certificate_attribute[0].expires
}

# Admin consent: the agent may call the MCP API on behalf of any customer (OBO).
resource "azuread_service_principal_delegated_permission_grant" "agent_to_mcp" {
  provider                             = azuread.external
  service_principal_object_id          = azuread_service_principal.agent_api.object_id
  resource_service_principal_object_id = azuread_service_principal.mcp_api.object_id
  claim_values                         = ["tools.access"]
}

resource "azuread_service_principal_delegated_permission_grant" "agent_to_graph" {
  provider                             = azuread.external
  service_principal_object_id          = azuread_service_principal.agent_api.object_id
  resource_service_principal_object_id = data.azuread_service_principal.msgraph.object_id
  claim_values                         = local.graph_scopes
}

# --- CLI (public client) -------------------------------------------------------

resource "azuread_application" "cli" {
  provider                       = azuread.external
  display_name                   = "banking-cli"
  sign_in_audience               = "AzureADMyOrg"
  fallback_public_client_enabled = true # device code flow; no redirect URI needed

  api {
    requested_access_token_version = 2 # external tenants reject v1 on any app
  }

  required_resource_access {
    resource_app_id = azuread_application.agent_api.client_id
    resource_access {
      id   = random_uuid.agent_scope.result
      type = "Scope"
    }
  }

  required_resource_access {
    resource_app_id = data.azuread_service_principal.msgraph.client_id
    dynamic "resource_access" {
      for_each = local.graph_scopes
      content {
        id   = data.azuread_service_principal.msgraph.oauth2_permission_scope_ids[resource_access.value]
        type = "Scope"
      }
    }
  }
}

resource "azuread_service_principal" "cli" {
  provider  = azuread.external
  client_id = azuread_application.cli.client_id
}

resource "azuread_application_pre_authorized" "cli" {
  provider             = azuread.external
  application_id       = azuread_application.agent_api.id
  authorized_client_id = azuread_application.cli.client_id
  permission_ids       = [random_uuid.agent_scope.result]
}

resource "azuread_service_principal_delegated_permission_grant" "cli_to_agent" {
  provider                             = azuread.external
  service_principal_object_id          = azuread_service_principal.cli.object_id
  resource_service_principal_object_id = azuread_service_principal.agent_api.object_id
  claim_values                         = ["access_as_user"]
}

resource "azuread_service_principal_delegated_permission_grant" "cli_to_graph" {
  provider                             = azuread.external
  service_principal_object_id          = azuread_service_principal.cli.object_id
  resource_service_principal_object_id = data.azuread_service_principal.msgraph.object_id
  claim_values                         = local.graph_scopes
}

# --- Key Vault with the OBO certificate (workforce subscription) --------------

resource "azurerm_key_vault" "main" {
  name                       = "kv-banking-${local.suffix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = false # dev: destroy must be able to purge
  soft_delete_retention_days = 7
  tags                       = local.tags
}

# You create the certificate through Terraform and read its private key for local runs.
resource "azurerm_role_assignment" "kv_certificates_officer_me" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Certificates Officer"
  principal_id         = data.azurerm_client_config.current.object_id
  principal_type       = "User"
}

# The private key of a Key Vault certificate is read through its backing secret.
resource "azurerm_role_assignment" "kv_secrets_user_me" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = data.azurerm_client_config.current.object_id
  principal_type       = "User"
}

resource "azurerm_role_assignment" "kv_secrets_user_agent_host" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.agent_host.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_key_vault_certificate" "obo" {
  name         = "agent-obo"
  key_vault_id = azurerm_key_vault.main.id

  certificate_policy {
    issuer_parameters {
      name = "Self"
    }
    key_properties {
      exportable = true
      key_size   = 2048
      key_type   = "RSA"
      reuse_key  = false
    }
    secret_properties {
      content_type = "application/x-pem-file" # the secret holds key + cert as PEM
    }
    x509_certificate_properties {
      subject            = "CN=banking-agent-api"
      validity_in_months = 12
      key_usage          = ["digitalSignature", "keyEncipherment"]
    }
  }

  # Data-plane RBAC must exist before Terraform can create the certificate.
  depends_on = [azurerm_role_assignment.kv_certificates_officer_me]
}
