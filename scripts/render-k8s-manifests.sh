#!/usr/bin/env bash
# scripts/render-k8s-manifests.sh
#
# Substitutes ${ACR_LOGIN_SERVER} (and any other supported env vars) in the
# raw Kubernetes manifests under kubernetes/services/ and writes the rendered
# output to kubernetes/services-rendered/. The rendered files are gitignored;
# this script is the bridge between the template manifests in git and the
# concrete manifests you `kubectl apply`.
#
# USAGE:
#   export ACR_LOGIN_SERVER="myacr.azurecr.io"   # FQDN of your ACR, no scheme
#   ./scripts/render-k8s-manifests.sh
#   kubectl apply -f kubernetes/services-rendered/
#
# The Helm path does NOT need this script — Helm interpolates image.repository
# from values.yaml at install time. See .github/workflows/deploy-services.yml
# for the production path: it passes --set image.repository=... directly.
#
# RATIONALE:
#   The original repo committed the literal string "REPLACE_ACR_LOGIN_SERVER"
#   into the image: fields. That meant `kubectl apply -f kubernetes/services/`
#   failed because no image registry on Earth answers to that hostname. By
#   switching to ${ACR_LOGIN_SERVER} we can use envsubst — a tiny tool ships
#   with gettext-base on every Linux distro and is available via Homebrew on
#   macOS — to produce a real manifest from the template.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${REPO_ROOT}/kubernetes/services"
OUT_DIR="${REPO_ROOT}/kubernetes/services-rendered"

if [[ -z "${ACR_LOGIN_SERVER:-}" ]]; then
  echo "ERROR: ACR_LOGIN_SERVER is not set." >&2
  echo "Find it with: az acr show -n <acr-name> --query loginServer -o tsv" >&2
  exit 1
fi

if ! command -v envsubst >/dev/null 2>&1; then
  echo "ERROR: envsubst not found. Install gettext-base (Linux) or gettext (mac)." >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"

# Only substitute the variables we explicitly support. Plain envsubst would
# clobber any literal $VAR in the manifests (e.g. in shell snippets inside
# ConfigMaps), which we don't want.
ALLOWED='${ACR_LOGIN_SERVER}'

shopt -s globstar nullglob
for src in "${SRC_DIR}"/**/*.yaml; do
  rel="${src#${SRC_DIR}/}"
  dst="${OUT_DIR}/${rel}"
  mkdir -p "$(dirname "${dst}")"
  envsubst "${ALLOWED}" < "${src}" > "${dst}"
  echo "rendered: ${rel}"
done

echo
echo "Rendered manifests written to ${OUT_DIR}"
echo "Next: kubectl apply -f kubernetes/services-rendered/"
