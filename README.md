# Meridian Commerce Platform — Multi-Region GKE, Akamai CDN, NGINX mTLS, Traffic Flip & SIEM/Bucket Observability

This repository contains the complete, production-ready implementation of the **Meridian Commerce Platform** (`meridian-commerce-platform`) on Google Cloud Platform (GCP), enhanced with four enterprise traffic, security, and observability patterns and engineered to run across four environments:

1. **Local Windows PC** (PowerShell + Python or Docker Desktop / Kind)
2. **Local Mac / Linux** (Bash + Python or Docker / Kind / Minikube)
3. **Google Colab / Jupyter Notebook** (`Meridian_Platform_Demo_Notebook.ipynb`)
4. **Google Cloud Platform (GCP Free Tier / Trial or Full Enterprise)** via modular **Terraform** (`terraform/`) and **Kubernetes Manifests** (`k8s/`)

## 1. Repository Directory Structure

```text
meridian-commerce-platform/
├── README.md                                  # Complete Architecture & Enhancement Reference
├── docker-compose.yml                         # Local 2-cluster, 2-app multi-container runner
├── run-local.ps1                              # Windows PowerShell verification & live web server runner
├── run-local.sh                               # macOS / Linux verification & live web server runner
├── Meridian_Platform_Demo_Notebook.ipynb      # Self-contained Google Colab / Jupyter Notebook demo
├── certs/                                     # Pre-generated X.509 Certificates for NGINX Ingress mTLS
│   ├── ca.crt                                 # Root CA Certificate (mounted into ingress-ca-secret)
│   ├── client.crt                             # Signed B2B Partner Client Certificate for mTLS testing
│   └── client.key                             # Client Private Key for curl --cert / --key testing
├── terraform/                                 # Deliverable 1: Infrastructure as Code (Terraform)
│   ├── versions.tf                            # Terraform & Google provider version constraints
│   ├── variables.tf                           # Configurable regions, machine types & free_tier_compatible flag
│   ├── vpc.tf                                 # Custom Global VPC, alias-IP subnets, Cloud NAT, Firewalls
│   ├── gke.tf                                 # Primary (us-central1) & Secondary (us-east1) GKE clusters + IAM
│   ├── observability_security.tf              # GCP Log Bucket, GCS Archive, SIEM Pub/Sub Sink & Akamai Origin Cloaking
│   ├── outputs.tf                             # Cluster endpoints, Global LB IP, Log Bucket & SIEM outputs
│   └── terraform.tfvars.example               # Template variables file for quick setup
├── apps/                                      # Deliverable 2: Two Stateless Web Applications
│   ├── storefront-ui/                         # Web App A: Storefront UI (Port 8080) with Drain Toggle & Akamai/mTLS headers
│   │   ├── app.py                             # Zero-dependency HTTP service with /readyz drain, True-Client-IP & mTLS logging
│   │   ├── Dockerfile                         # Non-root production container image
│   │   └── requirements.txt                   # Runtime requirements note
│   └── order-service/                         # Web App B: Order & Fulfillment API (Port 8081)
│       ├── app.py                             # Stateless REST API logging True-Client-IP, mTLS DN & trace hops
│       ├── Dockerfile                         # Non-root production container image
│       └── requirements.txt                   # Runtime requirements note
├── k8s/                                       # Deliverable 3: Multi-Pod GKE & Traffic Management Manifests
│   ├── 01-namespace-and-config.yaml           # Namespace, Workload Identity KSA, ConfigMap (with DRAIN_REGION), Secret
│   ├── 02-storefront-ui.yaml                  # App A Deployment (3->10 pods), ClusterIP + NEG annotation, HPA (65% CPU)
│   ├── 03-order-service.yaml                  # App B Deployment (3->8 pods), ClusterIP + NEG annotation, HPA (70% CPU)
│   ├── 04-ingress-and-mci.yaml                # Global External Ingress & Multi-Cluster Ingress (MCI) definitions
│   ├── 05-ingress-mtls-nginx.yaml             # Enhancement 2: Option B In-Cluster NGINX Ingress mTLS + ingress-ca-secret
│   ├── 06-advanced-regional-routing.yaml      # Enhancement 1: Intra-Region HTTPRoute (Header match, URL Rewrite, 90/10 Canary)
│   └── 07-akamai-backend-config.yaml          # Enhancement 3: BackendConfig binding Cloud Armor Akamai Origin Cloaking
├── observability/                             # Deliverable 4: GCP Log Bucket, SIEM & Grafana Analytics
│   ├── bigquery_queries.sql                   # BigQuery analyses over the two ingested JSONL tables
│   ├── big_queries.sql                        # Alternative queries for Cloud Logging's linked _AllLogs dataset
│   ├── grafana-dashboard.json                 # Importable 5-panel Grafana dashboard model
│   ├── sample_cloud_logs.jsonl                # Duplicate sample export of cloud_logs.jsonl
│   ├── cloud_logs.jsonl                       # Simulated multi-cluster JSONL telemetry
│   ├── live_docker_ingested_logs.jsonl        # Captured app logs plus four injected error scenarios
│   ├── grafana_dashboard_export.png           # Local 5-panel dashboard image with trace IDs
│   └── architecture_diagram.png               # High-res end-to-end architecture diagram
├── scripts/
│   ├── simulate_and_verify.py                 # End-to-end verifier for all 4 enhancements, SQL runner & chart builder
│   ├── traffic_flip_manager.ps1               # PowerShell CLI to drain/restore us-central1 (capacity-scaler & Method C)
│   └── traffic_flip_manager.sh                # Bash CLI to drain/restore us-central1 (capacity-scaler & Method C)
└── docs/                                      # Deliverable 5: Deep-Dive Documentation & Playbooks
    ├── ARCHITECTURE_AND_DESIGN.md             # Design decisions, rationale, and Free Tier adaptations
    ├── STEP_BY_STEP_GUIDE.md                  # Commands for Windows, Mac, Google Notebook, and GCP
    └── TROUBLESHOOTING_SCENARIO.md            # Root-cause analysis & resolution of GKE Ingress 502 & Sink IAM
```

## 2. Deep Dive: The 4 Enterprise Enhancements Implemented

### Enhancement 1: Regional Traffic Flip Management & Intra-Region Advanced Routing

1. **Disabling/Draining the Central Region (`us-central1`) Without Geo-DNS (`scripts/traffic_flip_manager.ps1` & `.sh`)**:
   - **GCP BackendService Level (`capacity-scaler = 0.0`)**: Drains the `us-central1` Network Endpoint Group (NEG) at the Google Cloud Global Load Balancer layer so 100% of requests route to `us-east1` regardless of client IP or DNS caching.
   - **Method C Readiness Drain Flag (`DRAIN_REGION: "true"` in `k8s/01-namespace-and-config.yaml` & `/api/admin/drain`)**: Both `apps/storefront-ui/app.py` and `apps/order-service/app.py` inspect `DRAIN_REGION` (and expose a live toggle button on `http://localhost:8080` calling `POST /api/admin/drain`). When enabled on `us-central1`, `/readyz` returns `HTTP 503 {"ready": false, "reason": "REGION_DRAINED_BY_OPERATOR", "failover_target": "us-east1"}`, causing GCLB or Akamai GTM health checks to immediately flip 100% of traffic to `us-east1`.
2. **Advanced Intra-Region Routing Rules (`k8s/06-advanced-regional-routing.yaml`)**:
   - **Rule 1 (Header-Based Routing)**: Requests carrying `X-Release-Track: canary` route exclusively to `storefront-ui-canary` pods (`port 8080`).
   - **Rule 2 (URL Path Rewrite)**: Requests to `/v1/catalog` are rewritten in-flight to `/api/catalog` via `ReplacePrefixMatch`.
   - **Rule 3 (Weighted Canary Split)**: Standard customer traffic within the active region is split **90% to `storefront-ui` (`stable`)** and **10% to `storefront-ui-canary` (`canary`)**.

### Enhancement 2: Option B — In-Cluster NGINX Ingress mTLS (`k8s/05-ingress-mtls-nginx.yaml` & `certs/`)

- **Pre-Generated X.509 PKI (`certs/ca.crt`, `certs/client.crt`, `certs/client.key`)**: Includes a valid Root CA (`CN=Meridian-Ingress-Root-CA`) and signed client certificate (`CN=b2b-partner-client,O=Meridian-Partner`).
- **Kubernetes Secret & NGINX Annotations (`k8s/05-ingress-mtls-nginx.yaml`)**:
  - Mounts `ca.crt` into `Secret/ingress-ca-secret` in `meridian-prod`.
  - Enforces strict mutual TLS via `nginx.ingress.kubernetes.io/auth-tls-verify-client: "on"`, `auth-tls-secret: "meridian-prod/ingress-ca-secret"`, and `auth-tls-pass-certificate-to-upstream: "true"`.
- **Application Identity Auditing**: Both `storefront-ui` and `order-service` extract the forwarded `Ssl-Client-Subject-Dn` header, display it in the Storefront security bar, and record `mtls_client_subject` in every structured log entry.

### Enhancement 3: Akamai CDN Approach + Origin Cloaking + `True-Client-IP` Preservation

- **Cloud Armor Origin Cloaking (`terraform/observability_security.tf` & `k8s/07-akamai-backend-config.yaml`)**:
  - `google_compute_security_policy.akamai_origin_cloaking_policy` (`meridian-akamai-origin-lockdown`) blocks direct-to-origin bypass attacks (`deny(403)` default) and allows traffic **only** when originating from Akamai Site Shield CIDRs (`inIpRange(origin.ip, '23.32.0.0/11')`) **and** carrying the secret header `x-akamai-origin-secret == 'mrd-akamai-edge-verified-9941'`.
- **End-to-End `True-Client-IP` & Trace Propagation (`apps/storefront-ui/app.py` $
ightarrow$ `apps/order-service/app.py`)**:
  - `storefront-ui` extracts Akamai's `True-Client-IP`, `Akamai-Request-BC` (breadcrumb), and `X-Cloud-Trace-Context` headers, displays `True-Client-IP` on the live Storefront UI, and forwards all three headers on east-west `POST /api/checkout` calls to `order-service`.

### Enhancement 4: Logs Ingestion to SIEM & GCP Buckets for Grafana Dashboard Creation

In `terraform/observability_security.tf`, Cloud Logging routes all container (`k8s_container`), cluster (`k8s_cluster`), node (`k8s_node`), and load balancer (`http_load_balancer`) logs into three parallel destinations:

| Log Destination Resource | Terraform Resource Name | Role in Observability & Grafana |
| :--- | :--- | :--- |
| **1. GCP Cloud Logging Analytics Bucket** | `google_logging_project_bucket_config.meridian_analytics_log_bucket` (`meridian-gke-log-bucket`) + `google_logging_linked_dataset.meridian_log_bucket_linked_bq` | Enables **Cloud Logging Log Analytics** (`enable_analytics = true`) and exposes the linked BigQuery dataset (`meridian_log_bucket_analytics._AllLogs`) for direct Cloud Logging analysis. |
| **2. GCP Cloud Storage (GCS) Archive Bucket** | `google_storage_bucket.meridian_log_archive_bucket` + `google_logging_project_sink.gcs_archive_sink` | Writes immutable JSONL log batches to a regional GCS bucket with a 90-day lifecycle policy for compliance and cold storage. |
| **3. Real-Time SIEM Pub/Sub Stream** | `google_pubsub_topic.siem_log_ingestion_topic` (`meridian-siem-log-stream`) + `google_logging_project_sink.siem_pubsub_sink` | Streams real-time logs (including Akamai `true_client_ip`, NGINX `mtls_client_subject`, and regional drain events) to Google SecOps Chronicle, Splunk, or Elastic SIEM. |

**Operational access and alerting notes (configure in the target GCP environment):**
- Cloud Monitoring alert policies can route notifications to distribution lists associated with the relevant ServiceNow support groups; map alert severity and affected service to the appropriate group.
- Restrict Secret Manager access through IAM grants to the designated administrator group, following least privilege. Application identities should receive access only to the specific secrets they require, not broad administrative permissions.

## 3. BigQuery Queries for Ingested GCS Logs (`observability/bigquery_queries.sql`)

The local simulator generates a rolling six-hour telemetry window and writes
`observability/cloud_logs.jsonl` plus its duplicate sample export
`observability/sample_cloud_logs.jsonl`. It refreshes four deterministic HTTP
5xx scenarios in `observability/live_docker_ingested_logs.jsonl` while
preserving captured records. The five-panel Grafana dashboard queries
`cloud_logs` and `live_docker_ingested_logs`, deduplicates the repeated
injected scenarios by trace ID, and includes an error/trace table. The local
dashboard image also includes that table. Use the generated files as local
simulation data; the simulator does not retrieve live Google Cloud logs.

Create external tables named `live_docker_ingested_logs` and `cloud_logs` in
the `cs-512516.logs_ingest` BigQuery dataset using the objects
`gs://<valid-bucket-name>/cs_cloud_logs/live_docker_ingested_logs.jsonl` and
`gs://<valid-bucket-name>/cs_cloud_logs/cloud_logs.jsonl`. Use your actual
valid GCS bucket name (bucket names cannot contain underscores), choose
`NEWLINE_DELIMITED_JSON`, and enable schema auto-detection in the BigQuery
console; SQL `CREATE EXTERNAL TABLE` does not support an `autodetect` option.
The script reads both external tables as
`cs-512516.logs_ingest.<table_name>`, normalizes the trace fields, and includes
a final error/trace research query. Run it as a multi-statement query. The
BigQuery service account must be able to read both objects.

`big_queries.sql` is retained as an alternative for projects that query the
Cloud Logging linked `_AllLogs` dataset directly; it is not the source for
the current Grafana dashboard.

## 4. Quickest Ways to Run & Demo All 4 Enhancements on Your Local PC

### Docker Compose (Windows, macOS, or Linux)

Start Docker Desktop (or the Docker Engine) and run these commands from the
repository root:
```shell
docker compose up --build -d
docker compose ps
```
Compose builds and starts both app services in both simulated clusters. Open
the storefronts at `http://localhost:8080` (primary) and
`http://localhost:8082` (secondary); the order-service endpoints are available
at ports `8081` and `8083`, respectively. To inspect container output, run
`docker compose logs -f`. Stop and remove the containers and network with
`docker compose down`.

### Windows PowerShell
```powershell
cd meridian-commerce-platform
.\run-local.ps1
```
- This runs the 4-enhancement verification suite, exports the updated Grafana and Architecture PNGs, starts `order-service` (`:8081`) and `storefront-ui` (`:8080`), and opens `http://localhost:8080`.
- On `http://localhost:8080`, click **"Toggle Region Drain (/readyz Failover)"** in the top-right bar (or run `.\scripts\traffic_flip_manager.ps1 -Action drain-local`) to flip `/readyz` to `HTTP 503` and test Method C regional failover live.
- Test the intra-region URL rewrite and canary header from PowerShell:
  ```powershell
  Invoke-RestMethod -Uri "http://localhost:8080/v1/catalog" -Headers @{"True-Client-IP"="198.51.100.77"; "X-Release-Track"="canary"; "Ssl-Client-Subject-Dn"="CN=b2b-partner-client,O=Meridian-Partner"} | ConvertTo-Json
  ```
