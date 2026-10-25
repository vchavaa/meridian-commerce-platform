param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,
    [string]$SecretName = "meridian-akamai-origin-secret"
)

$ErrorActionPreference = "Stop"
$secretOutput = @(gcloud secrets versions access latest --project $ProjectId --secret $SecretName)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to retrieve the Akamai origin secret from Secret Manager"
}

$originSecret = ($secretOutput -join "`n").TrimEnd([char[]]"`r`n")
if ($originSecret.Length -lt 32) {
    throw "The retrieved Akamai origin secret is empty or shorter than 32 characters"
}

$env:TF_VAR_akamai_origin_secret = $originSecret
Write-Host "Akamai origin secret is loaded into this PowerShell process for Terraform. Do not print or log it."
