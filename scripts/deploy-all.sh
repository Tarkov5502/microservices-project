#!/usr/bin/env bash
# scripts/deploy-all.sh
# Builds all Docker images and deploys all services.
# Usage: ./scripts/deploy-all.sh <acr-login-server> [image-tag]

set -euo pipefail

ACR="${1:?Usage: $0 <acr-login-server> [tag]}"
TAG="${2:-latest}"
NAMESPACE="microservices"

echo "🐳 Logging into ACR..."
az acr login --name "$(echo "$ACR" | cut -d. -f1)"

SERVICES=(api-gateway user-service task-service notification-service)

echo "🔨 Building and pushing images..."
for svc in "${SERVICES[@]}"; do
  echo "  → $svc:$TAG"
  docker build -t "$ACR/$svc:$TAG" "services/$svc/"
  docker push "$ACR/$svc:$TAG"
done

echo "🚀 Deploying with Helm..."
for svc in "${SERVICES[@]}"; do
  echo "  → deploying $svc"
  helm upgrade --install "$svc" "./helm/$svc" \
    --namespace "$NAMESPACE" \
    --set image.repository="$ACR/$svc" \
    --set image.tag="$TAG" \
    --wait --timeout=5m
done

echo ""
echo "✅ All services deployed! Checking pod status..."
kubectl get pods -n "$NAMESPACE"
