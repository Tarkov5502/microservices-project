# 🚀 Getting Started Guide

> **You are here**: You've just cloned the repo and have no idea where to begin.
> This guide walks you through everything from zero to a running cluster.

---

## Prerequisites

Install these tools before anything else:

| Tool | Why | Install |
|---|---|---|
| [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) | Manage Azure resources from terminal | `winget install Microsoft.AzureCLI` |
| [Terraform](https://developer.hashicorp.com/terraform/downloads) | Provision infrastructure | `winget install HashiCorp.Terraform` |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Manage Kubernetes cluster | `az aks install-cli` |
| [Helm](https://helm.sh/docs/intro/install/) | Deploy K8s packages | `winget install Helm.Helm` |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Build container images | Download from Docker website |
| [git](https://git-scm.com/) | Version control | `winget install Git.Git` |

Verify everything works:
```bash
az --version
terraform --version
kubectl version --client
helm version
docker --version
git --version
```

---

## Step 1: Azure Account Setup

### 1.1 Create a Free Azure Account

Go to https://azure.microsoft.com/free — you get $200 credit for 30 days.

> ⚠️ **Cost warning**: Running this full platform costs ~$150-250/month in dev configuration.
> Remember to `terraform destroy` when you're done learning to avoid unexpected charges!

### 1.2 Log in and set your subscription

```bash
az login
# A browser window opens — log in to your Azure account

# List your subscriptions
az account list --output table

# Set the one you want to use
az account set --subscription "YOUR_SUBSCRIPTION_ID_OR_NAME"

# Verify it's set correctly
az account show
```

---

## Step 2: Set Up Terraform State Storage

Before Terraform can manage state remotely, you need a place to store it.
Run this once — you never need to repeat it.

```bash
# Create a resource group for Terraform state
az group create \
  --name rg-terraform-state \
  --location eastus2

# Create a storage account (must be globally unique — add random suffix)
RANDOM_SUFFIX=$(openssl rand -hex 4)
STORAGE_ACCOUNT="stterraformstate${RANDOM_SUFFIX}"

az storage account create \
  --name $STORAGE_ACCOUNT \
  --resource-group rg-terraform-state \
  --sku Standard_LRS \
  --allow-blob-public-access false

# Create the blob container
az storage container create \
  --name tfstate \
  --account-name $STORAGE_ACCOUNT

echo "Storage account name: $STORAGE_ACCOUNT"
# SAVE THIS NAME — you'll need to put it in main.tf!
```

Now update the backend config in **both** environment files:
- `terraform/environments/dev/main.tf`
- `terraform/environments/prod/main.tf`

Change `storage_account_name = "stterraformstate"` to your actual storage account name.

---

## Step 3: Create GitHub Secrets

For the CI/CD pipelines to authenticate to Azure, add these secrets to your GitHub repo:

1. Go to `https://github.com/Tarkov5502/microservices-project/settings/secrets/actions`
2. Add the following secrets:

```bash
# Get your values:
az account show --query id -o tsv          # → AZURE_SUBSCRIPTION_ID
az account show --query tenantId -o tsv    # → AZURE_TENANT_ID

# Create a Service Principal for CI/CD:
az ad sp create-for-rbac \
  --name "sp-microservices-cicd" \
  --role "Contributor" \
  --scopes /subscriptions/YOUR_SUBSCRIPTION_ID \
  --json-auth
# The output gives you clientId → AZURE_CLIENT_ID
```

| Secret Name | Value |
|---|---|
| `AZURE_CLIENT_ID` | Service principal client ID |
| `AZURE_TENANT_ID` | Your Azure tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Your subscription ID |
| `ACR_NAME` | `acrmicroservicesdev` (set after Terraform apply) |
| `ACR_LOGIN_SERVER` | `acrmicroservicesdev.azurecr.io` |

---

## Step 4: Deploy Dev Infrastructure

```bash
cd terraform/environments/dev

# Download providers and initialize remote backend
terraform init

# Review what will be created (~50 resources, read carefully!)
terraform plan

# Deploy! (takes 10-15 minutes, mainly waiting for AKS)
terraform apply

# Save the outputs — you'll need these
terraform output
```

Expected outputs:
```
aks_cluster_name     = "aks-microservices-dev"
acr_login_server     = "acrmicroservicesdev.azurecr.io"
key_vault_name       = "kv-microservices-dev"
postgres_server_fqdn = "psql-microservices-dev.postgres.database.azure.com"
resource_group_name  = "rg-microservices-dev"
```

---

## Step 5: Connect kubectl to Your Cluster

```bash
az aks get-credentials \
  --resource-group rg-microservices-dev \
  --name aks-microservices-dev

# Verify connection
kubectl get nodes
# Should show: system node(s) in Ready state
```

---

## Step 6: Deploy Base Kubernetes Resources

```bash
cd ../../..  # Back to project root

# Create namespaces and resource quotas
kubectl apply -f kubernetes/namespaces/

# Create RBAC (ServiceAccounts, Roles, RoleBindings)
kubectl apply -f kubernetes/rbac/

# Apply network policies
kubectl apply -f kubernetes/network-policies/
```

---

## Step 7: Install NGINX Ingress Controller

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.replicaCount=2

# Get the external IP (wait a minute for Azure to provision the Load Balancer)
kubectl get service -n ingress-nginx ingress-nginx-controller -w
# When EXTERNAL-IP shows an IP (not <pending>), you're done!
```

---

## Step 8: Create Application Secrets in Kubernetes

The apps need connection strings from Key Vault. For now, let's manually create the K8s Secret:

```bash
# Fetch secrets from Key Vault
DB_CONN=$(az keyvault secret show \
  --vault-name kv-microservices-dev \
  --name db-connection-string \
  --query value -o tsv)

REDIS_CONN=$(az keyvault secret show \
  --vault-name kv-microservices-dev \
  --name redis-connection-string \
  --query value -o tsv)

SB_SENDER=$(az keyvault secret show \
  --vault-name kv-microservices-dev \
  --name sb-sender-connection-string \
  --query value -o tsv)

SB_LISTENER=$(az keyvault secret show \
  --vault-name kv-microservices-dev \
  --name sb-listener-connection-string \
  --query value -o tsv)

# Create K8s Secret
kubectl create secret generic app-secrets \
  --namespace microservices \
  --from-literal=db-connection-string="$DB_CONN" \
  --from-literal=redis-connection-string="$REDIS_CONN" \
  --from-literal=sb-sender-connection-string="$SB_SENDER" \
  --from-literal=sb-listener-connection-string="$SB_LISTENER" \
  --from-literal=jwt-secret="$(openssl rand -hex 32)"
```

---

## Step 9: Build and Push Docker Images

```bash
ACR_LOGIN_SERVER="acrmicroservicesdev.azurecr.io"
az acr login --name acrmicroservicesdev

for service in api-gateway user-service task-service notification-service; do
  echo "Building $service..."
  docker build -t $ACR_LOGIN_SERVER/$service:latest services/$service/
  docker push $ACR_LOGIN_SERVER/$service:latest
done
```

---

## Step 10: Deploy Services with Helm

```bash
ACR="acrmicroservicesdev.azurecr.io"

helm upgrade --install api-gateway ./helm/api-gateway \
  --namespace microservices \
  --set image.repository=$ACR/api-gateway \
  --set image.tag=latest

helm upgrade --install user-service ./helm/user-service \
  --namespace microservices \
  --set image.repository=$ACR/user-service \
  --set image.tag=latest

helm upgrade --install task-service ./helm/task-service \
  --namespace microservices \
  --set image.repository=$ACR/task-service \
  --set image.tag=latest

helm upgrade --install notification-service ./helm/notification-service \
  --namespace microservices \
  --set image.repository=$ACR/notification-service \
  --set image.tag=latest

# Watch pods come up
kubectl get pods -n microservices -w
```

---

## Step 11: Test the API

```bash
# Get the Ingress IP
INGRESS_IP=$(kubectl get service -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

# Test health endpoint (using IP directly, without domain)
curl http://$INGRESS_IP/health -H "Host: api.your-domain.com"
# Expected: {"status":"ok","service":"api-gateway"}

# Register a user
curl -X POST http://$INGRESS_IP/api/v1/auth/register \
  -H "Host: api.your-domain.com" \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","username":"testuser","password":"SecurePass1"}'

# Log in and get a JWT
TOKEN=$(curl -s -X POST http://$INGRESS_IP/api/v1/auth/login \
  -H "Host: api.your-domain.com" \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"SecurePass1"}' \
  | jq -r '.access_token')

echo "Your JWT: $TOKEN"
```

🎉 **Congratulations!** You have a running cloud-native microservices platform on Azure!

---

## Cleanup (Don't Forget!)

To avoid ongoing charges when you're done learning:

```bash
# Remove all Helm releases
helm uninstall api-gateway user-service task-service notification-service \
  -n microservices

# Destroy all Azure infrastructure (~$0 after this!)
cd terraform/environments/dev
terraform destroy
```
