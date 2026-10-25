#!/usr/bin/env bash
# =============================================================================
# Meridian Commerce Platform - Regional Traffic Flip Manager (Bash)
# Supports:
#   1. GCP BackendService capacity-scaler = 0.0 (Cloud Load Balancer Drain)
#   2. GKE ServiceExport deletion (Multi-Cluster Ingress Drain)
#   3. Method C ConfigMap / Live Readiness Drain (DRAIN_REGION=true -> HTTP 503)
# =============================================================================
set -euo pipefail

ACTION="${1:-status}"
PROJECT_ID="${PROJECT_ID:-cs-512516}"

case "$ACTION" in
  drain-central-gcp)
    echo "[Method A] Draining us-central1 NEG via GCP BackendService capacity-scaler=0.0..."
    BACKEND_SVC=$(gcloud compute backend-services list --project="$PROJECT_ID" --format="value(name)" --filter="name~storefront-ui" | head -n 1)
    if [[ -z "$BACKEND_SVC" ]]; then
      echo "No storefront backend service found in project $PROJECT_ID." >&2
      exit 1
    fi
    gcloud compute backend-services update-backend "$BACKEND_SVC" \
      --project="$PROJECT_ID" \
      --global \
      --network-endpoint-group="k8s1-meridian-storefront-ui-80-us-central1-a" \
      --network-endpoint-group-zone="us-central1-a" \
      --capacity-scaler=0.0
    echo "us-central1 capacity-scaler set to 0.0; verify global routing health before assuming failover."
    ;;
  restore-central)
    echo "Restoring us-central1 NEG capacity to 1.0..."
    BACKEND_SVC=$(gcloud compute backend-services list --project="$PROJECT_ID" --format="value(name)" --filter="name~storefront-ui" | head -n 1)
    if [[ -z "$BACKEND_SVC" ]]; then
      echo "No storefront backend service found in project $PROJECT_ID." >&2
      exit 1
    fi
    gcloud compute backend-services update-backend "$BACKEND_SVC" \
      --project="$PROJECT_ID" \
      --global \
      --network-endpoint-group="k8s1-meridian-storefront-ui-80-us-central1-a" \
      --network-endpoint-group-zone="us-central1-a" \
      --capacity-scaler=1.0
    ;;
  *)
    echo "Usage: PROJECT_ID=<project> $0 {drain-central-gcp|restore-central}"
    ;;
esac
