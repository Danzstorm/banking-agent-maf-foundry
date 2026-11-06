variable "subscription_id" {
  description = "Azure subscription ID to deploy into."
  type        = string
}

variable "location" {
  description = "Region for every resource. southcentralus offers gpt-5-mini GlobalStandard, Azure SQL provisioning, Container Apps and AI Search Free with agentic retrieval."
  type        = string
  default     = "southcentralus"
}

variable "alert_email" {
  description = "Email that receives budget alerts."
  type        = string
}

variable "client_ip" {
  description = "Your public IPv4 (curl -s https://api.ipify.org). Opens SQL for seeding and is the only IP allowed to reach the MCP app while AUTH_MODE=dev."
  type        = string

  validation {
    condition     = can(cidrhost("${var.client_ip}/32", 0))
    error_message = "client_ip must be a single IPv4 address, e.g. 203.0.113.10."
  }
}

variable "budget_amount" {
  description = "Monthly budget in USD for the resource group."
  type        = number
  default     = 20
}

variable "model_capacity" {
  description = "gpt-5-mini GlobalStandard capacity in thousands of tokens per minute (pay per token; capacity only caps throughput)."
  type        = number
  default     = 50
}

variable "model_version" {
  description = "gpt-5-mini model version (az cognitiveservices model list -l <region>)."
  type        = string
  default     = "2025-08-07"
}

variable "mcp_image" {
  description = "Container image for mcp-server. The public placeholder lets the first apply succeed before our image exists in ACR."
  type        = string
  default     = "mcr.microsoft.com/k8se/quickstart:latest"
}

variable "embedding_capacity" {
  description = "text-embedding-3-small GlobalStandard capacity in thousands of tokens per minute (pay per token)."
  type        = number
  default     = 50
}

variable "external_tenant_id" {
  description = "Entra External ID tenant (customers). Created manually once (ADR-008)."
  type        = string
}

variable "external_tenant_subdomain" {
  description = "Subdomain of the external tenant: <subdomain>.onmicrosoft.com and <subdomain>.ciamlogin.com."
  type        = string
}
