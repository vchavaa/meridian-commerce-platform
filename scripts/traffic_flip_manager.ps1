# =============================================================================
# Meridian Commerce Platform - Regional Traffic Flip Manager (PowerShell)
# =============================================================================
param(
    [ValidateSet("drain-local", "restore-local", "drain-central-gcp", "drain-central-k8s")]
    [string]$Action = "drain-local"
)

if ($Action -eq "drain-local") {
    Write-Host "[Method C] Draining Primary Cluster (us-central1) via /api/admin/drain?enable=true ..." -ForegroundColor Yellow
    Invoke-RestMethod -Method Post -Uri "http://localhost:8080/api/admin/drain?enable=true" | ConvertTo-Json
} elseif ($Action -eq "restore-local") {
    Write-Host "[Method C] Restoring Primary Cluster (us-central1) via /api/admin/drain?enable=false ..." -ForegroundColor Green
    Invoke-RestMethod -Method Post -Uri "http://localhost:8080/api/admin/drain?enable=false" | ConvertTo-Json
} elseif ($Action -eq "drain-central-gcp") {
    Write-Host "[Method A] Setting GCP BackendService capacity-scaler=0.0 for us-central1 NEG..." -ForegroundColor Cyan
    gcloud compute backend-services update-backend meridian-storefront-backend --global --network-endpoint-group=k8s1-meridian-storefront-ui-80-us-central1-a --network-endpoint-group-zone=us-central1-a --capacity-scaler=0.0
} elseif ($Action -eq "drain-central-k8s") {
    Write-Host "[Method C] Patching Kubernetes ConfigMap DRAIN_REGION=true in us-central1..." -ForegroundColor Cyan
    kubectl patch configmap meridian-platform-config -n meridian-prod --type merge -p '{"data":{"DRAIN_REGION":"true"}}'
}
