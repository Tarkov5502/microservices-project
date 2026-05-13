# 🏗️ Terraform Deep Dive

> Everything you need to understand how Terraform works and how this project uses it.

---

## What is Terraform?

Terraform is **Infrastructure as Code (IaC)** — you describe your infrastructure in `.tf` files, and Terraform makes reality match your description.

**Before IaC**: Click around in the Azure Portal. Forget what you clicked. Someone else's environment is different. Can't reproduce it.

**With Terraform**:
```hcl
resource "azurerm_resource_group" "main" {
  name     = "rg-microservices-dev"
  location = "eastus2"
}
```
Run `terraform apply` → Azure resource group exists. Every time. Everywhere. Reproducibly.

---

## Core Concepts

### 1. Providers

A provider is a plugin that knows how to talk to a specific API (Azure, AWS, Kubernetes, GitHub...).

```hcl
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"  # Downloaded from registry.terraform.io
      version = "~> 3.100"           # "~>" means "3.100 or any 3.x above it"
    }
  }
}

provider "azurerm" {
  features {}  # Required even if empty
}
```

Terraform downloads providers on `terraform init`. The `.terraform/` directory (gitignored) holds them.

### 2. Resources

A resource is one cloud object Terraform manages:

```hcl
resource "<PROVIDER>_<TYPE>" "<LOCAL_NAME>" {
  # arguments
}
```

```hcl
# Provider=azurerm, Type=resource_group, Local name=main
resource "azurerm_resource_group" "main" {
  name     = "rg-microservices-dev"
  location = "eastus2"
}

# Reference another resource: <TYPE>.<LOCAL_NAME>.<ATTRIBUTE>
resource "azurerm_virtual_network" "main" {
  resource_group_name = azurerm_resource_group.main.name  # reference!
  # ...
}
```

### 3. Variables and Outputs

Variables = inputs (like function parameters):
```hcl
variable "environment" {
  type    = string
  default = "dev"
}

# Use it: var.environment
resource "azurerm_resource_group" "main" {
  name = "rg-${var.environment}"
}
```

Outputs = return values (share data between modules):
```hcl
output "resource_group_name" {
  value = azurerm_resource_group.main.name
}
```

### 4. State

Terraform tracks what it has created in a **state file** (`terraform.tfstate`).

The state file is the source of truth: "What does Terraform currently manage?"

**Critical**: The state file can contain secrets! Never commit it to git!

**Remote state** (what we use): Store state in Azure Blob Storage with locking.

```hcl
backend "azurerm" {
  resource_group_name  = "rg-terraform-state"
  storage_account_name = "stterraformstate"
  container_name       = "tfstate"
  key                  = "microservices/dev/terraform.tfstate"
}
```

Benefits:
- Team members share the same state (no conflicts)
- State locking: only one `terraform apply` at a time (prevents corruption)
- Versioned blob storage: you can roll back state

### 5. Modules

Modules are reusable packages of Terraform configuration — like functions or libraries.

```
terraform/
├── modules/          ← Reusable building blocks (child modules)
│   ├── networking/
│   ├── aks/
│   └── database/
└── environments/     ← Root modules (callers)
    ├── dev/
    └── prod/
```

Calling a module:
```hcl
module "networking" {
  source = "../../modules/networking"    # Path to the module

  # Input variables
  project_name = var.project_name
  environment  = var.environment
  location     = var.location
}

# Using a module's output in another module call:
module "aks" {
  source        = "../../modules/aks"
  aks_subnet_id = module.networking.aks_subnet_id  # output from networking module
}
```

---

## Terraform Workflow

```
terraform init    → Download providers, initialize backend
terraform plan    → Show what WOULD change (dry run, read-only)
terraform apply   → Make changes (creates/modifies/destroys resources)
terraform destroy → Destroy ALL managed resources (careful!)
terraform output  → Show output values
terraform state   → Inspect/manipulate state
```

### The Plan is Your Friend

Always read the plan before applying:

```
# + means CREATE
+ resource "azurerm_resource_group" "main" {
    name     = "rg-microservices-dev"
    location = "eastus2"
  }

# ~ means MODIFY IN PLACE (safe)
~ resource "azurerm_kubernetes_cluster" "main" {
  ~ kubernetes_version = "1.28" -> "1.29"
  }

# -/+ means DESTROY AND RECREATE (dangerous! data loss possible!)
-/+ resource "azurerm_postgresql_flexible_server" "main" {
  ~ sku_name = "B_Standard_B1ms" -> "GP_Standard_D4s_v3"  # forces replacement
  }
```

The `-/+` is dangerous — it means destroying the database and creating a new empty one. Always look for this in your plan before applying!

---

## Setting Up Remote State (Do This First!)

Before running `terraform init` in any environment, you need an Azure Storage Account for state:

```bash
# Create a resource group for Terraform state storage
az group create --name rg-terraform-state --location eastus2

# Create storage account (name must be globally unique)
az storage account create \
  --name stterraformstate$(openssl rand -hex 4) \
  --resource-group rg-terraform-state \
  --sku Standard_LRS \
  --allow-blob-public-access false

# Create blob container
az storage container create \
  --name tfstate \
  --account-name <YOUR_STORAGE_ACCOUNT_NAME>

# Enable versioning (allows rolling back state)
az storage account blob-service-properties update \
  --account-name <YOUR_STORAGE_ACCOUNT_NAME> \
  --enable-versioning true
```

Update the `storage_account_name` in both `terraform/environments/dev/main.tf` and `prod/main.tf`.

---

## Deploying Dev Infrastructure

```bash
# 1. Authenticate to Azure
az login
az account set --subscription "YOUR_SUBSCRIPTION_ID"

# 2. Navigate to dev environment
cd terraform/environments/dev

# 3. Initialize (downloads providers, connects to remote state)
terraform init

# 4. Plan (see what will be created - ~50 resources)
terraform plan

# 5. Apply (takes ~10-15 minutes for AKS)
terraform apply

# 6. Get outputs (cluster name, ACR URL, etc.)
terraform output
```

---

## Common Terraform Mistakes

### Mistake 1: Hardcoding values
```hcl
# BAD — not reusable
name = "rg-microservices-dev"

# GOOD — parameterized
name = "rg-${var.project_name}-${var.environment}"
```

### Mistake 2: Committing tfstate
Add to `.gitignore`:
```
*.tfstate
*.tfstate.backup
.terraform/
```

### Mistake 3: Not pinning provider versions
```hcl
# BAD — might break when provider releases a major version
azurerm = { source = "hashicorp/azurerm" }

# GOOD — predictable
azurerm = { source = "hashicorp/azurerm", version = "~> 3.100" }
```

### Mistake 4: Running apply without reading the plan
```bash
# ALWAYS do this:
terraform plan -out=tfplan
# Review the plan carefully
terraform apply tfplan
```

---

## Terraform in CI/CD

Our GitHub Actions workflow (`deploy-infra.yml`) does:

1. **PRs**: `terraform plan` → posts output as PR comment (no changes made)
2. **Merge to main**: `terraform apply -auto-approve` on dev (automatic)
3. **Manual trigger**: `terraform apply` on prod (requires reviewer approval via GitHub Environments)

This pattern ensures:
- No surprise infrastructure changes (plan is always reviewed)
- Dev stays up to date automatically
- Prod requires a human to approve
