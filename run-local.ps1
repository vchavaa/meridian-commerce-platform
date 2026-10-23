# =============================================================================
# Meridian Commerce Platform - Local Demo & Live Web Server Runner (Windows)
# =============================================================================
$ErrorActionPreference = "Stop"

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host " Meridian Commerce Platform - Running Verification & Starting Web " -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

# Step 1: Run automated verification, BigQuery SQL analytics, and chart export
python scripts/simulate_and_verify.py

# Step 2: Start Web Application B (order-service) on http://localhost:8081
Write-Host "`nStarting Web App B (order-service) in background on http://localhost:8081 ..." -ForegroundColor Green
$orderProc = Start-Process python -ArgumentList "apps/order-service/app.py" -PassThru -NoNewWindow

# Step 3: Start Web Application A (storefront-ui) on http://localhost:8080
Write-Host "Starting Web App A (storefront-ui) on http://localhost:8080 ..." -ForegroundColor Green
Start-Sleep -Seconds 1
Start-Process "http://localhost:8080"

Write-Host "`n==================================================================" -ForegroundColor Yellow
Write-Host " LIVE WEB ENDPOINTS ARE NOW RUNNING:" -ForegroundColor Yellow
Write-Host " -> Storefront UI (Web App A) : http://localhost:8080" -ForegroundColor White
Write-Host " -> Catalog JSON API          : http://localhost:8080/api/catalog" -ForegroundColor White
Write-Host " -> Storefront Health Check   : http://localhost:8080/healthz" -ForegroundColor White
Write-Host " -> Order API (Web App B)     : http://localhost:8081/api/orders" -ForegroundColor White
Write-Host " -> Order API Health Check    : http://localhost:8081/healthz" -ForegroundColor White
Write-Host " Press Ctrl+C to stop the web servers." -ForegroundColor Yellow
Write-Host "==================================================================`n" -ForegroundColor Yellow

try {
    python apps/storefront-ui/app.py
} finally {
    if ($orderProc -and !$orderProc.HasExited) {
        Stop-Process -Id $orderProc.Id -Force
    }
}
