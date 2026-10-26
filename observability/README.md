# Observability — Grafana Panels, BigQuery Queries, and Data Sources

This folder holds the dashboard JSON, exported screenshot, BigQuery SQL suite, and the simulated JSONL corpus that back each panel. Nothing here contains live GCP telemetry; every panel is reproducible from either (a) the simulated JSONL files below or (b) a live `logs_ingest` dataset after `terraform apply` populates the log sinks.

## Data sources

| File | Rows | Window | Origin |
|---|---|---|---|
| `cloud_logs.jsonl` | 5,128 | 6-hour synthetic window | Generated deterministically by `scripts/simulate_and_verify.py`. Models cross-cluster application/platform events with injected 5xx scenarios. |
| `live_docker_ingested_logs.jsonl` | varies | last `docker compose` run | Captured by piping container stdout through `jq`; see Step-by-Step Guide §Local run. |

Both files are **inputs** to the BigQuery external tables `PROJECT_ID.logs_ingest.cloud_logs` and `PROJECT_ID.logs_ingest.live_docker_ingested_logs` once `terraform apply` + GCS upload is performed. The queries below work identically against the local JSONL corpus (via BigQuery sandbox / DuckDB) and the live tables.

## Panel ↔ query map

| Panel # | Title | SQL section in `bigquery_queries.sql` | Simulated-data range | Live-data range |
|---|---|---|---|---|
| 1 | HTTP 5xx Error Rate (%) Over Time | `-- PANEL 1: Application Error Rates Over Time` | Peaks at 21.6% during injected inventory-lock scenario (trace `a1010...0002`) | Depends on real traffic |
| 2 | Pod Restart Counts by Namespace (24h) | `-- PANEL 2: Pod Restart Counts by Namespace & Cluster` | 4 synthetic restart records (OOMKilled + BackOff) across order-service and storefront-ui | Depends on real pod lifecycle events |
| 3 | Request Latency Percentiles (p50/p95/p99) | `-- PANEL 3: Request Latency Percentiles (p50, p95, p99)` | Baseline p50 ~34-42 ms; spikes to 602 ms (lock event) and 850 ms (injected scenario `0004`) | Depends on real latency distribution |
| 4 | Regional CPU (%) & Traffic Flip | `-- PANEL 4: Resource Utilization Trends (CPU % & Memory MB)` | Models drain of us-central1 (CPU drops to ~40%) with us-east1 absorbing failover (sustains ~70%) | Depends on real HPA + drain events |
| 5 | HTTP Error Scenarios & Trace IDs | `-- PANEL 5: HTTP 5xx Trace Explorer (Panel 5)` (see file) | 4 injected trace IDs `a1010...0001` through `0004` | Any real 5xx trace surfaces here |

The 5 trace IDs appear exactly twice each (once in `cloud_logs.jsonl`, once in `live_docker_ingested_logs.jsonl`) so cross-source dedup logic can be demonstrated in Panel 5.

## Importing the dashboard

Grafana Cloud or self-hosted:

```bash
# From Grafana UI: Dashboards -> Import -> Upload JSON file
#   Upload observability/grafana-dashboard.json
#   Pick the BigQuery data source bound to the active PROJECT_ID
```

Data-source expectations:

- BigQuery plugin (Grafana Cloud: pre-installed; self-hosted: `grafana-bigquery-datasource` plugin).
- Service account with `roles/bigquery.dataViewer` + `roles/bigquery.jobUser` on the project.
- Default dataset: `logs_ingest`.

## Regenerating the simulated corpus

```bash
python scripts/simulate_and_verify.py
# Writes: observability/cloud_logs.jsonl (5,128 rows) + regenerated screenshots.
```

The script is deterministic — same inputs produce identical trace IDs and distributions. If a reviewer needs a different distribution, pass `--seed <int>`.

## Running the queries against a live `logs_ingest` dataset

```bash
# 1. Create the dataset:
bq --project_id=PROJECT_ID mk --location=us logs_ingest

# 2. Create the external tables (JSONL in a GCS bucket):
bq --project_id=PROJECT_ID mk --external_table_definition=NEWLINE_DELIMITED_JSON=gs://<bucket>/cs_cloud_logs/cloud_logs.jsonl \
   logs_ingest.cloud_logs
bq --project_id=PROJECT_ID mk --external_table_definition=NEWLINE_DELIMITED_JSON=gs://<bucket>/cs_cloud_logs/live_docker_ingested_logs.jsonl \
   logs_ingest.live_docker_ingested_logs

# 3. Run the panel queries:
bq --project_id=PROJECT_ID query --use_legacy_sql=false < observability/bigquery_queries.sql
```
