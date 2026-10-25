# ============================================================================
# 1. Project Governance & Networking: Custom VPC, Subnets, Cloud NAT, Firewalls
# ============================================================================

resource "google_project_service" "enabled_apis" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudtrace.googleapis.com",
    "cloudprofiler.googleapis.com",
    "bigquery.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "sqladmin.googleapis.com",
    "dns.googleapis.com",
    "iam.googleapis.com"
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

resource "google_compute_network" "meridian_vpc" {
  name                    = "meridian-${var.environment}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "GLOBAL"
  description             = "Global VPC for Meridian multi-region GKE clusters and observability infrastructure"
  depends_on              = [google_project_service.enabled_apis]
}

# Primary Region Subnet (us-central1) with Secondary Alias IP Ranges for VPC-Native GKE
resource "google_compute_subnetwork" "gke_primary_subnet" {
  name                     = "gke-primary-subnet-${var.primary_region}"
  ip_cidr_range            = "10.10.0.0/20"
  region                   = var.primary_region
  network                  = google_compute_network.meridian_vpc.id
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "gke-primary-pods"
    ip_cidr_range = "10.100.0.0/16"
  }

  secondary_ip_range {
    range_name    = "gke-primary-services"
    ip_cidr_range = "10.101.0.0/20"
  }

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Secondary Region Subnet (us-east1) with Symmetric Layout
resource "google_compute_subnetwork" "gke_secondary_subnet" {
  name                     = "gke-secondary-subnet-${var.secondary_region}"
  ip_cidr_range            = "10.20.0.0/20"
  region                   = var.secondary_region
  network                  = google_compute_network.meridian_vpc.id
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "gke-secondary-pods"
    ip_cidr_range = "10.200.0.0/16"
  }

  secondary_ip_range {
    range_name    = "gke-secondary-services"
    ip_cidr_range = "10.201.0.0/20"
  }

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Dedicated Ops / Monitoring Subnet
resource "google_compute_subnetwork" "ops_monitoring_subnet" {
  name                     = "ops-monitoring-subnet-${var.primary_region}"
  ip_cidr_range            = "10.30.0.0/24"
  region                   = var.primary_region
  network                  = google_compute_network.meridian_vpc.id
  private_ip_google_access = true
}

# Cloud Router & Cloud NAT (Primary Region)
resource "google_compute_router" "primary_router" {
  name    = "meridian-router-${var.primary_region}"
  region  = var.primary_region
  network = google_compute_network.meridian_vpc.id
}

resource "google_compute_router_nat" "primary_nat" {
  name                               = "meridian-nat-${var.primary_region}"
  router                             = google_compute_router.primary_router.name
  region                             = var.primary_region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Cloud Router & Cloud NAT (Secondary Region)
resource "google_compute_router" "secondary_router" {
  name    = "meridian-router-${var.secondary_region}"
  region  = var.secondary_region
  network = google_compute_network.meridian_vpc.id
}

resource "google_compute_router_nat" "secondary_nat" {
  name                               = "meridian-nat-${var.secondary_region}"
  router                             = google_compute_router.secondary_router.name
  region                             = var.secondary_region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Firewall: Allow Google Cloud Load Balancer Health Checks (130.211.0.0/22, 35.191.0.0/16) to GKE NEGs
resource "google_compute_firewall" "allow_gclb_health_checks" {
  name        = "meridian-allow-gclb-health-checks"
  network     = google_compute_network.meridian_vpc.name
  description = "Allow Google Global Load Balancer and Ingress health check probes to reach container ports"
  direction   = "INGRESS"
  priority    = 1000

  allow {
    protocol = "tcp"
    ports    = ["80", "443", "8080", "10256"]
  }

  source_ranges = ["130.211.0.0/22", "35.191.0.0/16"]
  target_tags   = ["gke-meridian-node"]
}

# Firewall: Allow East-West Internal Communication Between Primary and Secondary Subnets
resource "google_compute_firewall" "allow_east_west_internal" {
  name        = "meridian-allow-east-west-internal"
  network     = google_compute_network.meridian_vpc.name
  description = "Allow inter-cluster east-west communication across primary and secondary GKE subnets"
  direction   = "INGRESS"
  priority    = 1010

  allow {
    protocol = "tcp"
    ports    = ["8080", "8081"]
  }

  source_ranges = [
    "10.10.0.0/20",
    "10.100.0.0/16",
    "10.20.0.0/20",
    "10.200.0.0/16"
  ]
}
