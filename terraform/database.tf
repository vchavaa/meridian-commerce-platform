resource "google_sql_database_instance" "orders" {
  name                = "meridian-orders-db"
  database_version    = "POSTGRES_16"
  region              = var.primary_region
  deletion_protection = true

  lifecycle {
    precondition {
      condition     = var.primary_region != var.secondary_region
      error_message = "Cloud SQL backup location must differ from the primary region."
    }
  }

  settings {
    tier              = var.cloudsql_tier
    availability_type = "REGIONAL"
    disk_type         = "PD_SSD"
    disk_autoresize   = true
    backup_configuration {
      start_time                     = "02:00"
      location                       = var.secondary_region
      enabled                        = true
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 7
        retention_unit   = "COUNT"
      }
    }

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    ip_configuration {
      ipv4_enabled = true
      require_ssl  = true
    }
  }

  depends_on = [
    google_project_service.enabled_apis,
    google_project_iam_member.app_workload_roles
  ]
}

resource "google_sql_database" "orders" {
  name     = "meridian"
  instance = google_sql_database_instance.orders.name
}

resource "google_sql_user" "app_workload" {
  name     = google_service_account.app_workload_sa.email
  instance = google_sql_database_instance.orders.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}
