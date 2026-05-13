# terraform.tfvars — actual values for dev
# This file is environment-specific. DO NOT commit secrets here!
# Sensitive values (passwords, keys) live in Key Vault, not this file.

project_name       = "microservices"
environment        = "dev"
location           = "eastus2"
kubernetes_version = "1.29"

tags = {
  Owner      = "platform-team"
  CostCenter = "engineering"
  Purpose    = "learning"
}
