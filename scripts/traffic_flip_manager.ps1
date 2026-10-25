# =============================================================================
# Meridian Commerce Platform - Regional Traffic Flip Manager (PowerShell)
# =============================================================================
param(
    [ValidateSet("drain-local", "restore-local", "drain-central-gcp")]
    [string]$Action = "drain-local",
    [string]$AdminUri = "http://localhost:8080"
)

if ($Action -eq "drain-local") {
    if (-not $env:LOCAL_ADMIN_TOKEN) {
        throw "Set LOCAL_ADMIN_TOKEN to the token printed by run-local.ps1."
    }
    Write-Host "Draining the configured local region using authenticated admin API..." -ForegroundColor Yellow
    $headers = @{ Authorization = "Bearer $env:LOCAL_ADMIN_TOKEN" }
    Invoke-RestMethod -Method Post -Uri "$AdminUri/api/admin/drain" -Headers $headers `
        -ContentType "application/json" -Body '{"drain":true}' | ConvertTo-Json
} elseif ($Action -eq "restore-local") {
    if (-not $env:LOCAL_ADMIN_TOKEN) {
        throw "Set LOCAL_ADMIN_TOKEN to the token printed by run-local.ps1."
    }
    Write-Host "Restoring the configured local region using authenticated admin API..." -ForegroundColor Green
    $headers = @{ Authorization = "Bearer $env:LOCAL_ADMIN_TOKEN" }
    Invoke-RestMethod -Method Post -Uri "$AdminUri/api/admin/drain" -Headers $headers `
        -ContentType "application/json" -Body '{"drain":false}' | ConvertTo-Json
} elseif ($Action -eq "drain-central-gcp") {
    Write-Host "[Method A] Setting GCP BackendService capacity-scaler=0.0 for us-central1 NEG..." -ForegroundColor Cyan
    gcloud compute backend-services update-backend meridian-storefront-backend --global --network-endpoint-group=k8s1-meridian-storefront-ui-80-us-central1-a --network-endpoint-group-zone=us-central1-a --capacity-scaler=0.0
}
