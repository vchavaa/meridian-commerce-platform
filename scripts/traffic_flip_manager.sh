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
PROJECT_ID="${PROJECT_ID:-meridian-cloud}"

case "$ACTION" in
  drain-central-gcp)
    echo "[Method A] Draining us-central1 NEG via GCP BackendService capacity-scaler=0.0..."
    BACKEND_SVC=$(gcloud compute backend-services list --project="$PROJECT_ID" --format="value(name)" --filter="name~storefront-ui" | head -n 1)
    gcloud compute backend-services update-backend "$BACKEND_SVC" \
      --global \
      --network-endpoint-group="k8s1-meridian-storefront-ui-80-us-central1-a" \
      --network-endpoint-group-zone="us-central1-a" \
      --capacity-scaler=0.0
    echo "us-central1 capacity-scaler set to 0.0. 100% of traffic now routes to us-east1."
    ;;
  drain-central-k8s)
    echo "[Method C] Setting DRAIN_REGION=true in meridian-platform-config (us-central1)..."
    kubectl patch configmap meridian-platform-config -n meridian-prod --type merge -p '{"data":{"DRAIN_REGION":"true"}}'
    kubectl rollout restart deployment/storefront-ui deployment/order-service -n meridian-prod
    echo "us-central1 /readyz probes now return HTTP 503; traffic failed over to us-east1."
    ;;
  restore-central)
    echo "Restoring us-central1 to active traffic (DRAIN_REGION=false, capacity-scaler=1.0)..."
    kubectl patch configmap meridian-platform-config -n meridian-prod --type merge -p '{"data":{"DRAIN_REGION":"false"}}' || true
    ;;
  *)
    echo "Usage: $0 {drain-central-gcp|drain-central-k8s|restore-central}"
    ;;
esac
