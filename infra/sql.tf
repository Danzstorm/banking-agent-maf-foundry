resource "azurerm_mssql_server" "main" {
  name                = "sql-banking-agent-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  version             = "12.0"
  minimum_tls_version = "1.2"
  tags                = local.tags

  # Entra-only: no SQL logins or passwords exist. The admin is whoever runs terraform.
  azuread_administrator {
    login_username              = data.azuread_user.current.user_principal_name
    object_id                   = data.azurerm_client_config.current.object_id
    tenant_id                   = data.azurerm_client_config.current.tenant_id
    azuread_authentication_only = true
  }
}

# azurerm does not expose the free offer (useFreeLimit), so the database is managed with azapi.
resource "azapi_resource" "sql_db" {
  type      = "Microsoft.Sql/servers/databases@2023-08-01"
  name      = "sqldb-banking"
  parent_id = azurerm_mssql_server.main.id
  location  = azurerm_resource_group.main.location
  tags      = local.tags

  body = {
    sku = {
      name     = "GP_S_Gen5"
      tier     = "GeneralPurpose"
      family   = "Gen5"
      capacity = 2
    }
    properties = {
      useFreeLimit                     = true
      freeLimitExhaustionBehavior      = "AutoPause" # never bills: pauses until next month
      minCapacity                      = 0.5
      maxSizeBytes                     = 34359738368 # 32 GB, the free limit
      zoneRedundant                    = false
      requestedBackupStorageRedundancy = "Local"
    }
  }

  response_export_values = ["name"]
}

resource "azurerm_mssql_firewall_rule" "azure_services" {
  name             = "AllowAzureServices" # 0.0.0.0 = any Azure service (Container Apps egress)
  server_id        = azurerm_mssql_server.main.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

resource "azurerm_mssql_firewall_rule" "client" {
  name             = "AllowClientIp"
  server_id        = azurerm_mssql_server.main.id
  start_ip_address = var.client_ip
  end_ip_address   = var.client_ip
}
