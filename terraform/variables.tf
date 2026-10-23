variable "project_id" {
  description = "GCP Project ID where Meridian Commerce Platform resources will be provisioned."
  type        = string
}

variable "environment" {
  description = "Deployment environment label (e.g., prod, stage, dev)."
  type        = string
  default     = "prod"
}

variable "primary_region" {
  description = "Primary GCP region for Cluster 1 (Active Primary)."
  type        = string
  default     = "us-central1"
}

variable "primary_zone" {
  description = "Primary GCP zone when running zonal clusters to conserve GCP Free Tier quota."
  type        = string
  default     = "us-central1-a"
}

variable "secondary_region" {
  description = "Secondary GCP region for Cluster 2 (Active/Active or Disaster Recovery)."
  type        = string
  default     = "us-east1"
}

variable "secondary_zone" {
  description = "Secondary GCP zone when running zonal clusters to conserve GCP Free Tier quota."
  type        = string
  default     = "us-east1-b"
}

variable "free_tier_compatible" {
  description = "When true, provisions zonal GKE Standard clusters with e2-medium spot/standard nodes and skips paid-only Fleet MCI APIs to fit within GCP Free Trial quotas."
  type        = bool
  default     = true
}

variable "node_machine_type" {
  description = "Compute Engine machine type for GKE worker nodes."
  type        = string
  default     = "e2-medium"
}

variable "min_nodes_per_Cluster" {
  description = "Minimum nodes in the general workload pool per cluster."
  type        = number
  default     = 1
}

variable "max_nodes_per_cluster" {
  description = "Maximum nodes in the general workload pool per cluster."
  type        = number
  default     = 3
}
