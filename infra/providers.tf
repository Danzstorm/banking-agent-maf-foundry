provider "azurerm" {
  subscription_id = var.subscription_id
  # Storage data-plane calls use Entra ID: the knowledge storage account disables shared keys.
  storage_use_azuread = true

  features {
    cognitive_account {
      # AIServices accounts are soft-deleted; purge so destroy/apply cycles stay clean.
      purge_soft_delete_on_destroy = true
    }
  }
}

provider "azapi" {}

provider "azuread" {}
