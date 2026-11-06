# --- Phase 3: knowledge base (ADR-012) ---------------------------------------
# Storage holds the rendered product PDFs; AI Search Free hosts the Foundry IQ knowledge source
# and knowledge base. Knowledge source and knowledge base are data-plane objects created by
# knowledge/ingest.py, not by Terraform.

resource "azurerm_storage_account" "knowledge" {
  name                            = "stbankingagent${local.suffix}"
  resource_group_name             = azurerm_resource_group.main.name
  location                        = azurerm_resource_group.main.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false # Entra ID only
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

# storage_account_id makes the provider create the container through ARM, which works with
# shared keys disabled.
resource "azurerm_storage_container" "knowledge" {
  name                  = "productos"
  storage_account_id    = azurerm_storage_account.knowledge.id
  container_access_type = "private"
}

# The docs list managed identity as Basic+, but ARM accepts it on Free (probed 2026-11-06), and
# the knowledge source needs it: Foundry has local auth disabled, so there is no key to use.
resource "azurerm_search_service" "main" {
  name                         = "srch-banking-agent-${local.suffix}"
  resource_group_name          = azurerm_resource_group.main.name
  location                     = azurerm_resource_group.main.location
  sku                          = "free"
  local_authentication_enabled = false # Entra ID (RBAC) only
  tags                         = local.tags

  identity {
    type = "SystemAssigned"
  }

  lifecycle {
    ignore_changes = [semantic_search_sku] # owned by azapi_update_resource.search_semantic
  }
}

# Knowledge base retrieval fails with "requires Semantic Search" unless the semantic ranker is
# on. The API accepts the free plan on the Free tier, but azurerm rejects semantic_search_sku
# with sku = "free", so it is set through azapi.
resource "azapi_update_resource" "search_semantic" {
  type        = "Microsoft.Search/searchServices@2025-05-01"
  resource_id = azurerm_search_service.main.id
  body = {
    properties = { semanticSearch = "free" }
  }
}

# Embeddings for the chunks the knowledge source indexes and for query vectorization.
resource "azurerm_cognitive_deployment" "embedding" {
  name                 = "text-embedding-3-small"
  cognitive_account_id = azurerm_cognitive_account.foundry.id

  model {
    format  = "OpenAI"
    name    = "text-embedding-3-small"
    version = "1"
  }

  sku {
    name     = "GlobalStandard"
    capacity = var.embedding_capacity
  }
}

# --- Roles ---------------------------------------------------------------------

# Search reads the PDFs (indexer) and calls the embedding model (skillset and vectorizer).
resource "azurerm_role_assignment" "search_blob_reader" {
  scope                = azurerm_storage_account.knowledge.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_search_service.main.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "search_foundry" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_search_service.main.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# You upload the PDFs and create the knowledge source and knowledge base.
resource "azurerm_role_assignment" "blob_contributor_me" {
  scope                = azurerm_storage_account.knowledge.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
  principal_type       = "User"
}

resource "azurerm_role_assignment" "search_service_contributor_me" {
  scope                = azurerm_search_service.main.id
  role_definition_name = "Search Service Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
  principal_type       = "User"
}

# Index Data Contributor also covers retrieval (read) for local runs and evaluations.
resource "azurerm_role_assignment" "search_data_contributor_me" {
  scope                = azurerm_search_service.main.id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
  principal_type       = "User"
}

# The agent host only queries the knowledge base.
resource "azurerm_role_assignment" "search_data_reader_agent_host" {
  scope                = azurerm_search_service.main.id
  role_definition_name = "Search Index Data Reader"
  principal_id         = azurerm_user_assigned_identity.agent_host.principal_id
  principal_type       = "ServicePrincipal"
}
