provider "azurerm" {
  subscription_id = var.subscription_id

  features {
    cognitive_account {
      # AIServices accounts are soft-deleted; purge so destroy/apply cycles stay clean.
      purge_soft_delete_on_destroy = true
    }
  }
}

provider "azapi" {}

provider "azuread" {}
