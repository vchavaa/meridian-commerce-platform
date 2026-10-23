# ============================================================================
# 2. GKE Cluster Architecture: Primary (us-central1) & Secondary (us-east1)
# ============================================================================

# Dedicated Least-Privilege Node Service Account
resource "google_service_account" "gke_node_sa" {
  account_id   = "meridian-gke-node-sa"
  display_name = "Meridian GKE Node Pool Service Account"
}

resource "google_project_iam_member" "gke_node_roles" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
    "roles/artifactregistry.reader",
    "roles/cloudtrace.agent",
    "roles/cloudprofiler.agent"
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.gke_node_sa.email}"
}

# Workload Identity Service Account for Application Pods (Web App A & B)
resource "google_service_account" "app_workload_sa" {
  account_id   = "meridian-app-workload-sa"
  display_name = "Meridian Web Apps Workload Identity Service Account"
}

resource "google_project_iam_member" "app_workload_roles" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
    "roles/cloudprofiler.agent",
    "roles/secretmanager.secretAccessor"
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.app_workload_sa.email}"
}

# Bind Kubernetes ServiceAccount (meridian-prod/meridian-app-ksa) to GCP Service Account
resource "google_service_account_iam_member" "workload_identity_binding" {
  service_account_id = google_service_account.app_workload_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[meridian-prod/meridian-app-ksa]"
  depends_on         = [google_container_cluster.primary_cluster]
}

# ----------------------------------------------------------------------------
# Cluster 1: Primary GKE Cluster (us-central1)
# ----------------------------------------------------------------------------
resource "google_container_cluster" "primary_cluster" {
  name     = "meridian-gke-primary"
  location = var.free_tier_compatible ? var.primary_zone : var.primary_region

  network    = google_compute_network.meridian_vpc.name
  subnetwork = google_compute_subnetwork.gke_primary_subnet.name

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {
    cluster_secondary_range_name  = "gke-primary-pods"
    services_secondary_range_name = "gke-primary-services"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = true
    }
  }

  release_channel {
    channel = "REGULAR"
  }
}

resource "google_container_node_pool" "primary_general_pool" {
  name       = "general-web-pool"
  location   = google_container_cluster.primary_cluster.location
  cluster    = google_container_cluster.primary_cluster.name
  node_count = var.min_nodes_per_Cluster

  autoscaling {
    min_node_count = var.min_nodes_per_Cluster
    max_node_count = var.max_nodes_per_cluster
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.node_machine_type
    disk_size_gb    = 30
    disk_type       = "pd-standard"
    spot            = var.free_tier_compatible
    service_account = google_service_account.gke_node_sa.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    tags            = ["gke-meridian-node", "primary-cluster"]

    labels = {
      environment = var.environment
      role        = "web-workloads"
      cluster     = "primary"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

# ----------------------------------------------------------------------------
# Cluster 2: Secondary GKE Cluster (us-east1)
# ----------------------------------------------------------------------------
resource "google_container_cluster" "secondary_cluster" {
  name     = "meridian-gke-secondary"
  location = var.free_tier_compatible ? var.secondary_zone : var.secondary_region

  network    = google_compute_network.meridian_vpc.name
  subnetwork = google_compute_subnetwork.gke_secondary_subnet.name

  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {
    cluster_secondary_range_name  = "gke-secondary-pods"
    services_secondary_range_name = "gke-secondary-services"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = true
    }
  }

  release_channel {
    channel = "REGULAR"
  }
}

resource "google_container_node_pool" "secondary_general_pool" {
  name       = "general-web-pool"
  location   = google_container_cluster.secondary_cluster.location
  cluster    = google_container_cluster.secondary_cluster.name
  node_count = var.min_nodes_per_Cluster

  autoscaling {
    min_node_count = var.min_nodes_per_Cluster
    max_node_count = var.max_nodes_per_cluster
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.node_machine_type
    disk_size_gb    = 30
    disk_type       = "pd-standard"
    spot            = var.free_tier_compatible
    service_account = google_service_account.gke_node_sa.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    tags            = ["gke-meridian-node", "secondary-cluster"]

    labels = {
      environment = var.environment
      role        = "web-workloads"
      cluster     = "secondary"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}
