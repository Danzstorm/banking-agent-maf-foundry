data "azurerm_client_config" "current" {}

data "azuread_user" "current" {
  object_id = data.azurerm_client_config.current.object_id
}

resource "random_string" "suffix" {
  length  = 5
  special = false
  upper   = false
}

locals {
  suffix = random_string.suffix.result
  tags = {
    project = "banking-agent"
    env     = "dev"
  }
}

resource "azurerm_resource_group" "main" {
  name     = "rg-banking-agent-dev"
  location = var.location
  tags     = local.tags
}

# --- Identities -------------------------------------------------------------

# Used by the mcp-server container to pull from ACR and connect to Azure SQL.
resource "azurerm_user_assigned_identity" "mcp_server" {
  name                = "id-mcp-server"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

# Used by the agent host (phase 2) to call the Foundry project.
resource "azurerm_user_assigned_identity" "agent_host" {
  name                = "id-agent-host"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

# --- Budget -----------------------------------------------------------------

resource "azurerm_consumption_budget_resource_group" "main" {
  name              = "budget-banking-agent-dev"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.budget_amount
  time_grain        = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  notification {
    operator       = "GreaterThanOrEqualTo"
    threshold      = 50
    threshold_type = "Actual"
    contact_emails = [var.alert_email]
  }

  notification {
    operator       = "GreaterThanOrEqualTo"
    threshold      = 100
    threshold_type = "Actual"
    contact_emails = [var.alert_email]
  }

  notification {
    operator       = "GreaterThanOrEqualTo"
    threshold      = 100
    threshold_type = "Forecasted"
    contact_emails = [var.alert_email]
  }

  lifecycle {
    # start_date is computed once at creation; avoid a diff on every plan.
    ignore_changes = [time_period]
  }
}
