# --- GitHub Actions identity for the on-demand evaluations (ADR-020) ------------
# OIDC only: GitHub exchanges its workflow token for an Entra token through the federated
# credential. No client secret exists. Only workflows on main of this repository qualify.

resource "azuread_application" "github_evals" {
  display_name     = "banking-github-evals"
  sign_in_audience = "AzureADMyOrg"
}

resource "azuread_service_principal" "github_evals" {
  client_id = azuread_application.github_evals.client_id
}

resource "azuread_application_federated_identity_credential" "github_main" {
  application_id = azuread_application.github_evals.id
  display_name   = "github-main"
  description    = "GitHub Actions, ${var.github_repository}, branch main."
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:${var.github_repository}:ref:refs/heads/main"
}

# The same two data-plane roles the agent host uses for the model and the knowledge base. Foundry
# User also covers logging evaluation and red team runs to the project. No control-plane role.
resource "azurerm_role_assignment" "foundry_user_github" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Foundry User"
  principal_id         = azuread_service_principal.github_evals.object_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "search_data_reader_github" {
  scope                = azurerm_search_service.main.id
  role_definition_name = "Search Index Data Reader"
  principal_id         = azuread_service_principal.github_evals.object_id
  principal_type       = "ServicePrincipal"
}
