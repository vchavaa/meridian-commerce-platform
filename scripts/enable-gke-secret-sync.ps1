param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,
    [Parameter(Mandatory = $true)]
    [string]$PrimaryLocation,
    [Parameter(Mandatory = $true)]
    [string]$SecondaryLocation
)

$ErrorActionPreference = "Stop"
$clusters = @(
    @{ Name = "meridian-gke-primary"; Location = $PrimaryLocation },
    @{ Name = "meridian-gke-secondary"; Location = $SecondaryLocation }
)

foreach ($cluster in $clusters) {
    gcloud container clusters update $cluster.Name `
        --project $ProjectId `
        --location $cluster.Location `
        --enable-secret-sync `
        --enable-secret-sync-rotation `
        --secret-sync-rotation-interval=120s
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to enable Secret Manager synchronization for $($cluster.Name)"
    }
}
