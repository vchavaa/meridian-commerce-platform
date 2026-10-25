locals {
  ingress_secret_ids = toset([
    "meridian-public-tls-cert",
    "meridian-public-tls-key",
    "meridian-partner-tls-cert",
    "meridian-partner-tls-key",
    "meridian-partner-client-ca"
  ])
}

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_secret_manager_secret" "ingress" {
  for_each  = local.ingress_secret_ids
  secret_id = each.value

  replication {
    user_managed {
      replicas {
        location = var.primary_region
      }
      replicas {
        location = var.secondary_region
      }
    }
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_iam_member" "ingress_sync_access" {
  for_each  = google_secret_manager_secret.ingress
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "principal://iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/meridian-prod/sa/meridian-secret-sync-ksa"
}

data "google_secret_manager_secret" "akamai_origin" {
  secret_id = var.akamai_origin_secret_name
}
