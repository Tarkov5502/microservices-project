# Production Deployment Guide

> Deploy the platform to Azure. Estimated time: 45–90 minutes on first run.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Azure CLI | ≥ 2.58 | `winget install Microsoft.AzureCLI` |
| Terraform | ≥ 1.7 | https://developer.hashicorp.com/terraform/install |
| kubectl | ≥ 1.29 | `az aks install-cli` |
| Helm | ≥ 3.14 | https://helm.sh/docs/intro/install/ |
| Docker | ≥ 25 | https://docs.docker.com/get-docker/ |

---

## Phase 1 — Azure Account Setup

```bash
# Log in to Azure
az login

# List your subscriptions and note the ID
az account list --output table

# Set the subscription you want to deploy to
az account set --subscription "YOUR_SUBSCRIPTION_ID"

# Verify
az account show --output table
```

---

## Phase 2 — Terraform Remote State Bootstrap

Terraform stores its state (what infrastructure exists) in Azure Blob Storage.
This must be created manually once before Terraform can run.

```bash
# Create a resource group for Terraform state
az group create \
  --name rg-terraform-state \
  --location eastus2

# Create a storage account (name must be globally unique)
az storage account create \
  --name tfstate$(openssl rand -hex 4) \
  --resource-group rg-terraform-state \
  --sku Standard_LRS \
  --allow-blob-public-access false

# Note the storage account name from the output above, then create the container
az storage container create \
  --name tfstate \
  --account-name <YOUR_STORAGE_ACCOUNT_NAME>
```

Now update `terraform/environments/dev/main.tf` with your storage account name:
```hcl
backend "azurerm" {
  resource_group_name  = "rg-terraform-state"
  storage_account_name = "<YOUR_STORAGE_ACCOUNT_NAME>"   # ← update this
  container_name       = "tfstate"
  key                  = "dev.terraform.tfstate"
}
```

---

## Phase 3 — Deploy Infrastructure with Terraform

```bash
cd terraform/environments/dev

# Initialize Terraform (downloads providers, connects to remote state)
terraform init

# See what will be created (safe — read only)
terraform plan

# Deploy! This creates:
#   - Virtual Network + Subnets + NSGs
#   - AKS Cluster (takes ~10 minutes)
#   - Azure Container Registry
#   - PostgreSQL Flexible Server
#   - Azure Cache for Redis
#   - Azure Service Bus
#   - Azure Key Vault
#   - Log Analytics Workspace
terraform apply
```

**Expected time**: 15–25 minutes. Get a coffee.

```bash
# After apply completes, capture the outputs
terraform output
# Note: acr_login_server, aks_cluster_name, resource_group_name
```

---

## Phase 4 — Build and Push Docker Images

```bash
# Set variables from Terraform output
ACR_NAME=$(terraform output -raw acr_name)
ACR_SERVER=$(terraform output -raw acr_login_server)

# CR
az acr login --name $ACR_NAME

# Build and push all four images
cd ../../..   # back to project root
IMAGE_TAG=$(git rev-parse --short HEAD)

for svc in api-gateway user-service task-service notification-service; do
  docker build -t $ACR_SERVER/$svc:$IMAGE_TAG services/$svc/
  docker push $ACR_SERVER/$svc:$IMAGE_TAG
done
```

---

## Phase 5 — Connect kubectl to AKS

```bash
RG=$(terraform -chdir=terraform/environments/dev output -raw resource_group_name)
CLUSTER=$(terraform -chdir=terraform/environments/dev output -raw aks_cluster_name)

az aks get-credentials \
  --resource-group $RG \
  --name $CLUSTER \
  --overwrite-existing

# Verify connectivity
kubectl get nodes
```

---

## Phase 6 — Deploy Kubernetes Base Resources

```bash
# Create namespaces, ResourceQuotas, LimitRanges
kubectl apply -f kubernetes/namespaces/

# Create RBAC roles and service accounts
kubectl apply -f kubernetes/rbac/

# Apply network policies (microsegmentation)
kubectl apply -f kubernetes/network-policies/

# Create PodDisruptionBudgets (HA during node maintenance)
kubectl apply -f kubernetes/disruption-budgets/

# Set up ingress controller
kubectl apply -f kubernetes/ingress/
```

---

## Phase 7 — Create Kubernetes Secrets

Secrets are fetched from Key Vault and stored in K8s Secrets for pod access.
The AKS Key Vault integration (enabled in Terraform) mounts them as files,
but for Helm chart env vars we use K8s Secrets directly.

```bash
# Get connection strin Key Vault
KV_NAME=$(terraform -chdir=terraform/environments/dev output -raw key_vault_name)

DB_USER_CONN=$(az keyvault secret show --vault-name $KV_NAME --name db-user-connection-string --query value -o tsv)
DB_TASK_CONN=$(az keyvault secret show --vault-name $KV_NAME --name db-task-connection-string --query value -o tsv)
REDIS_CONN=$(az keyvault secret show --vault-name $KV_NAME --name redis-connection-string --query value -o tsv)
SB_SENDER=$(az keyvault secret show --vault-name $KV_NAME --name sb-sender-connection-string --query value -o tsv)
SB_LISTENER=$(az keyvault secret show --vault-name $KV_NAME --name sb-listener-connection-string --query value -o tsv)

# Generate a strong JWT secret
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# Store in Key Vault for reference
az keyvault secret set --vault-name $KV_NAME --name jwt-secret --value $JWT_SECRET

# Create K8s secret in the microservices namespace
kubectl create secret generic microservices-secrets \
  --namespace microservices \
  --from-literal=db-connection-string="$DB_USER_CONN" \
  --from-literal=db-task-connection-string="$DB_TASK_CONN" \
  --from-literal=redis-connection-string="$REDIS_CONN" \
  --from-literal=sb-sender-connection-string="$SB_SENDER" \
  --from-literal=sb-listener-connection-string="$SB_LISTENER" \
  --from-literal=jwt-secret="$JWT_SECRET"
```

---

## Phase 8 — Deploy Services with Helm

```bash
ACR_SERVER=$(terraform -chdir=terraform/environments/dev output -raw acr_login_server)
IMAGE_TAG=$(git rev-parse --short HEAD)

for svc in api-gateway user-service task-service notification-service; do
  helm upgrade --install $svc ./helm/$svc \
    --namespace microservices \
    --set image.repository=$ACR_SERVER/$svc \
    --set image.tag=$IMAGE_TAG \
    --wait \
    --timeout=5m
done

# Verify all pods are running
kubectl get pods -n microservices
```

---

## Phase 9 — Install Monitoring

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --wait

# Apply ServiceMonitors and alert rules
kubectl apply -f kubernetes/monitoring/

# Get Grafana password
kubectl get secret prometheus-grafana \
  --namespace monitoring \
  -o jsonpath="{.data.admin-password}" | base64 -d
```

---

## Phase 10 — Verify Deployment

```bash
# Check everything is healthy
kubectl get pods -n microservices
kubectl get pods -n monitoring
kubectl get ingress -n microservices

# Get the external IP of the ingress
kubectl get svc -n ingress-nginx

# Test the health endpoint
INGRESS_IP=$(kubectl get svc ingress-nginx-controller -n ingress-nginx -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
curl http://$INGRESS_IP/health
```

---

## CI/CD — Automated Deployments

After the first manual deploy, subsequent changes deploy automatically:

| Trigger | Action |
|---------|--------|
| Open a PR with Terraform changes | `terraform plan` posted as PR comment |
| Merge to `main` with Terraform changes | `terraform apply dev` automatically |
| Merge to `main` with service changes | Build images, `helm upgrade` changed services only |
| Manual workflow dispatch | `terraform apply prod` (requires reviewer approval) |

See `.github/workflows/` for the full pipeline definitions.

---

## Destroying the Dev Environment

```bash
cd terraform/environments/dev
terraform destroy
```

⚠️ This deletes EVERYTHING — AKS cluster, databases, all data. There is no undo.
Use with care. The `prod` environment has a manual approval gate in CI that prevents accidental destruction.
