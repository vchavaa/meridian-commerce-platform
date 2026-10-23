output "vpc_name" {
  description = "Provisioned VPC Network name"
  value       = google_compute_network.meridian_vpc.name
}

output "primary_cluster_name" {
  description = "Primary GKE Cluster Name (us-central1)"
  value       = google_container_cluster.primary_cluster.name
}

output "primary_cluster_endpoint" {
  description = "Primary GKE Control Plane Endpoint"
  value       = google_container_cluster.primary_cluster.endpoint
  sensitive   = true
}

output "secondary_cluster_name" {
  description = "Secondary GKE Cluster Name (us-east1)"
  value       = google_container_cluster.secondary_cluster.name
}

output "secondary_cluster_endpoint" {
  description = "Secondary GKE Control Plane Endpoint"
  value       = google_container_cluster.secondary_cluster.endpoint
  sensitive   = true
}

output "global_load_balancer_ip" {
  description = "Global Static IP address for DNS A-record mapping"
  value       = google_compute_global_address.global_ingress_ip.address
}

output "bigquery_dataset_id" {
  description = "BigQuery Dataset receiving GKE and Application logs"
  value       = google_bigquery_dataset.gke_logs_dataset.dataset_id
}

output "artifact_registry_url" {
  description = "Artifact Registry Docker repository URL"
  value       = "${var.primary_region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.meridian_repo.repository_id}"
}
