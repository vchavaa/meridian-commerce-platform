# =============================================================================
# Meridian Commerce Platform - Local Demo & Live Web Server Runner (Windows)
# =============================================================================
$ErrorActionPreference = "Stop"

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host " Meridian Commerce Platform - Running Verification & Starting Web " -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

# Step 1: Run automated verification, BigQuery SQL analytics, and chart export.
python scripts/simulate_and_verify.py
if ($LASTEXITCODE -ne 0) {
    throw "Local verification failed with exit code $LASTEXITCODE"
}

$random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$tokenBytes = New-Object byte[] 32
$random.GetBytes($tokenBytes)
$env:LOCAL_ADMIN_TOKEN = [Convert]::ToBase64String($tokenBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$random.Dispose()

Write-Host "`nStarting Docker Compose services. Local admin token: $env:LOCAL_ADMIN_TOKEN" -ForegroundColor Yellow
Write-Host "The generated token is local to this run. Do not reuse it in a cloud deployment." -ForegroundColor Yellow

Write-Host "`n==================================================================" -ForegroundColor Yellow
Write-Host " LOCAL WEB ENDPOINTS:" -ForegroundColor Yellow
Write-Host " -> Storefront UI (Web App A) : http://localhost:8080" -ForegroundColor White
Write-Host " -> Catalog JSON API          : http://localhost:8080/api/catalog" -ForegroundColor White
Write-Host " -> Storefront Health Check   : http://localhost:8080/healthz" -ForegroundColor White
Write-Host " -> Order API (Web App B)     : http://localhost:8081/api/orders" -ForegroundColor White
Write-Host " -> Order API Health Check   : http://localhost:8081/healthz" -ForegroundColor White
Write-Host " Press Ctrl+C to stop the containers. PostgreSQL data remains in its named volume." -ForegroundColor Yellow
Write-Host "==================================================================`n" -ForegroundColor Yellow

try {
    docker compose up --build --detach
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose exited with code $LASTEXITCODE"
    }
    Start-Process "http://localhost:8080"
    docker compose logs --follow
} finally {
    docker compose down
    Remove-Item Env:\LOCAL_ADMIN_TOKEN -ErrorAction SilentlyContinue
}
