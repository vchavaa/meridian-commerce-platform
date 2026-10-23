# ============================================================================
# 5. Observability, SIEM/Bucket Log Sinks, & Akamai Edge Security
# ============================================================================

# Artifact Registry for Container Images
resource "google_artifact_registry_repository" "meridian_repo" {
  location      = var.primary_region
  repository_id = "meridian-containers"
  description   = "Container registry for Storefront UI (App A) and Order Service (App B)"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled_apis]
}

# ----------------------------------------------------------------------------
# A. GCP Cloud Logging Bucket with Log Analytics Enabled (For Grafana Queries)
# ----------------------------------------------------------------------------
resource "google_logging_project_bucket_config" "meridian_analytics_log_bucket" {
  project          = var.project_id
  location         = "global"
  bucket_id        = "meridian-gke-log-bucket"
  retention_days   = 30
  enable_analytics = true
  description      = "Centralized GCP Cloud Logging Bucket with Log Analytics enabled for Grafana and SQL querying"
}

# Link the Cloud Logging Analytics Bucket to a BigQuery Dataset for Grafana SQL
resource "google_logging_linked_dataset" "meridian_log_bucket_linked_bq" {
  link_id     = "meridian_log_bucket_analytics"
  bucket      = google_logging_project_bucket_config.meridian_analytics_log_bucket.id
  description = "Linked BigQuery dataset exposing the GCP Cloud Logging Bucket directly to Grafana"
}

# Route GKE Container, Pod, Node, and Ingress Logs into the GCP Logging Bucket
resource "google_logging_project_sink" "log_bucket_sink" {
  name        = "meridian-to-gcp-log-bucket-sink"
  destination = "logging.googleapis.com/projects/${var.project_id}/locations/global/buckets/${google_logging_project_bucket_config.meridian_analytics_log_bucket.bucket_id}"

  filter = <<-EOT
    resource.type=("k8s_container" OR "k8s_cluster" OR "k8s_node" OR "k8s_pod" OR "http_load_balancer")
  EOT

  unique_writer_identity = true
}

# ----------------------------------------------------------------------------
# B. GCP Cloud Storage (GCS) Bucket for Long-Term Compliance & Log Archival
# ----------------------------------------------------------------------------
resource "google_storage_bucket" "meridian_log_archive_bucket" {
  name                        = "${var.project_id}-meridian-siem-log-archive"
  location                    = "US"
  force_destroy               = true
  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_logging_project_sink" "gcs_archive_sink" {
  name        = "meridian-to-gcs-bucket-sink"
  destination = "storage.googleapis.com/${google_storage_bucket.meridian_log_archive_bucket.name}"

  filter = <<-EOT
    resource.type=("k8s_container" OR "k8s_cluster" OR "k8s_node" OR "k8s_pod" OR "http_load_balancer")
  EOT

  unique_writer_identity = true
}

resource "google_storage_bucket_iam_member" "gcs_sink_writer" {
  bucket = google_storage_bucket.meridian_log_archive_bucket.name
  role   = "roles/storage.objectCreator"
  member = google_logging_project_sink.gcs_archive_sink.writer_identity
}

# ----------------------------------------------------------------------------
# C. SIEM Real-Time Log Ingestion Pipeline (Cloud Pub/Sub Topic + Sink)
#    Streams structured GKE & Akamai logs to Chronicle SIEM / Splunk / Grafana
# ----------------------------------------------------------------------------
resource "google_pubsub_topic" "siem_log_ingestion_topic" {
  name                       = "meridian-siem-log-stream"
  message_retention_duration = "86400s"
}

resource "google_pubsub_subscription" "siem_log_subscription" {
  name                 = "meridian-siem-collector-sub"
  topic                = google_pubsub_topic.siem_log_ingestion_topic.name
  ack_deadline_seconds = 20
}

resource "google_logging_project_sink" "siem_pubsub_sink" {
  name        = "meridian-to-siem-pubsub-sink"
  destination = "pubsub.googleapis.com/projects/${var.project_id}/topics/${google_pubsub_topic.siem_log_ingestion_topic.name}"

  filter = <<-EOT
    resource.type=("k8s_container" OR "k8s_cluster" OR "http_load_balancer")
  EOT

  unique_writer_identity = true
}

resource "google_pubsub_topic_iam_member" "siem_sink_publisher" {
  topic  = google_pubsub_topic.siem_log_ingestion_topic.name
  role   = "roles/pubsub.publisher"
  member = google_logging_project_sink.siem_pubsub_sink.writer_identity
}

# ----------------------------------------------------------------------------
# D. BigQuery Dataset & Sink for Direct Grafana SQL Analytics
# ----------------------------------------------------------------------------
resource "google_bigquery_dataset" "gke_logs_dataset" {
  dataset_id                  = "meridian_gke_observability"
  friendly_name               = "Meridian GKE & Application Observability Logs"
  description                 = "Centralized sink destination for GKE container, control plane, and Akamai/LB logs"
  location                    = "US"
  default_table_expiration_ms = 2592000000 # 30 days retention
  delete_contents_on_destroy  = true
  depends_on                  = [google_project_service.enabled_apis]
}

resource "google_logging_project_sink" "bigquery_log_sink" {
  name        = "meridian-gke-to-bigquery-sink"
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.gke_logs_dataset.dataset_id}"

  filter = <<-EOT
    resource.type=("k8s_container" OR "k8s_cluster" OR "k8s_node" OR "k8s_pod" OR "http_load_balancer")
  EOT

  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_bigquery_dataset_iam_member" "sink_bq_writer" {
  dataset_id = google_bigquery_dataset.gke_logs_dataset.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.bigquery_log_sink.writer_identity
}

# Secret Manager Secret for Application B (Order Service)
resource "google_secret_manager_secret" "order_service_secret" {
  secret_id = "meridian-order-service-config"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_version" "order_service_secret_v1" {
  secret      = google_secret_manager_secret.order_service_secret.id
  secret_data = jsonencode({
    payment_gateway_key  = "pk_live_meridian_98a7bc65d4e3"
    akamai_origin_secret = "mrd-akamai-edge-verified-9941"
    redis_pool_timeout   = 5
  })
}

# Global Static IP for External HTTPS Load Balancer / Ingress
resource "google_compute_global_address" "global_ingress_ip" {
  name        = "meridian-global-lb-ip"
  description = "Global anycast IP for Meridian multi-cluster traffic entry"
  depends_on  = [google_project_service.enabled_apis]
}

# ----------------------------------------------------------------------------
# E. Cloud Armor Policy: Akamai CDN Origin Cloaking + L7 WAF Fallback
# ----------------------------------------------------------------------------
resource "google_compute_security_policy" "akamai_origin_cloaking_policy" {
  name        = "meridian-akamai-origin-lockdown"
  description = "Origin Cloaking: Allows only verified Akamai Site Shield CIDRs with X-Akamai-Origin-Secret header"

  # Rule 1000: Block SQLi even on origin pulls (Defense in Depth)
  rule {
    action   = "deny(403)"
    priority = 1000
    match {
      expr {
        expression = "evaluatePreconfiguredExpr('sqli-v33-stable')"
      }
    }
    description = "Defense-in-depth SQLi check at GCP origin"
  }

  # Rule 1010: Block XSS payloads at GCP origin
  rule {
    action   = "deny(403)"
    priority = 1010
    match {
      expr {
        expression = "evaluatePreconfiguredExpr('xss-v33-stable')"
      }
    }
    description = "Defense-in-depth XSS check at GCP origin"
  }

  # Rule 1100: Allow verified Akamai Site Shield Edge Servers carrying secret origin header
  rule {
    action   = "allow"
    priority = 1100
    match {
      expr {
        expression = "inIpRange(origin.ip, '23.32.0.0/11') && request.headers['x-akamai-origin-secret'] == 'mrd-akamai-edge-verified-9941'"
      }
    }
    description = "Allow verified Akamai Site Shield origin pulls"
  }

  # Rule 2147483647: Default Deny all direct-to-origin bypass attempts
  rule {
    action   = "deny(403)"
    priority = 2147483647
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    description = "Block direct internet traffic bypassing Akamai CDN"
  }
}
