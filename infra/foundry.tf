resource "azurerm_cognitive_account" "foundry" {
  name                       = "aif-banking-agent-${local.suffix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  kind                       = "AIServices"
  sku_name                   = "S0"
  custom_subdomain_name      = "aif-banking-agent-${local.suffix}"
  project_management_enabled = true
  local_auth_enabled         = false # Entra ID only, no API keys
  tags                       = local.tags

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_cognitive_account_project" "main" {
  name                 = "proj-banking-agent"
  cognitive_account_id = azurerm_cognitive_account.foundry.id
  location             = azurerm_resource_group.main.location
  display_name         = "Banking agent"
  description          = "Banco Andino Demo banking agent (MAF + Foundry)."
  tags                 = local.tags

  identity {
    type = "SystemAssigned"
  }
}

# DefaultV2 plus Prompt Shields: jailbreak (direct) and indirect attack (documents), both blocking.
# The harm categories restate the DefaultV2 values (Medium, blocking) so the policy is explicit.
resource "azurerm_cognitive_account_rai_policy" "main" {
  name                 = "banking-agent-guardrails"
  cognitive_account_id = azurerm_cognitive_account.foundry.id
  base_policy_name     = "Microsoft.DefaultV2"
  tags                 = local.tags

  dynamic "content_filter" {
    for_each = flatten([
      for category in ["Hate", "Sexual", "Violence", "Selfharm"] : [
        for source in ["Prompt", "Completion"] : { name = category, source = source }
      ]
    ])
    content {
      name               = content_filter.value.name
      source             = content_filter.value.source
      filter_enabled     = true
      block_enabled      = true
      severity_threshold = "Medium"
    }
  }

  content_filter {
    name               = "Jailbreak"
    source             = "Prompt"
    filter_enabled     = true
    block_enabled      = true
    severity_threshold = "High" # required by the schema; ignored for binary classifiers
  }

  content_filter {
    name               = "Indirect Attack"
    source             = "Prompt"
    filter_enabled     = true
    block_enabled      = true
    severity_threshold = "High"
  }

  content_filter {
    name               = "Protected Material Text"
    source             = "Completion"
    filter_enabled     = true
    block_enabled      = true
    severity_threshold = "High"
  }

  content_filter {
    name               = "Protected Material Code"
    source             = "Completion"
    filter_enabled     = true
    block_enabled      = true
    severity_threshold = "High"
  }
}

resource "azurerm_cognitive_deployment" "gpt5mini" {
  name                 = "gpt-5-mini"
  cognitive_account_id = azurerm_cognitive_account.foundry.id
  rai_policy_name      = azurerm_cognitive_account_rai_policy.main.name

  model {
    format  = "OpenAI"
    name    = "gpt-5-mini"
    version = var.model_version
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.model_capacity
  }
}

# "Foundry User" (formerly "Azure AI User"): data-plane access to the account and its projects
# (Responses API, tracing, evaluations). Local auth is disabled, so its listkeys action is moot.
resource "azurerm_role_assignment" "foundry_user_agent_host" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Foundry User"
  principal_id         = azurerm_user_assigned_identity.agent_host.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "foundry_user_me" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Foundry User"
  principal_id         = data.azurerm_client_config.current.object_id
  principal_type       = "User"
}
