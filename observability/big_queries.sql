-- ============================================================================
-- MERIDIAN COMMERCE PLATFORM: ALTERNATIVE CLOUD LOGGING LINKED-DATASET QUERIES
-- This older query set reads Cloud Logging's linked _AllLogs dataset directly.
-- The current external-JSONL workflow and Grafana dashboard use
-- observability/bigquery_queries.sql and cs-512516.logs_ingest tables instead.
-- Primary Source : `PROJECT_ID.meridian_log_bucket_analytics._AllLogs`
--                  (Linked Dataset on GCP Logging Bucket `meridian-gke-log-bucket`)
-- Secondary Sink : `PROJECT_ID.meridian_gke_observability._AllLogs` & SIEM Stream
-- ============================================================================

-- ----------------------------------------------------------------------------
-- PANEL 1: Application Error Rates Over Time (Queried from GCP Log Bucket)
-- Includes Regional Drain / Traffic Flip Window & Canary vs Stable Breakdown
-- ----------------------------------------------------------------------------
SELECT
  TIMESTAMP_TRUNC(timestamp, MINUTE) AS time_bucket,
  jsonPayload.service AS service_name,
  jsonPayload.cluster_name AS cluster_name,
  jsonPayload.release_track AS release_track,
  COUNT(*) AS total_requests,
  COUNTIF(severity = 'ERROR' OR CAST(jsonPayload.status_code AS INT64) >= 500) AS error_count,
  ROUND(
    SAFE_DIVIDE(
      COUNTIF(severity = 'ERROR' OR CAST(jsonPayload.status_code AS INT64) >= 500) * 100.0,
      COUNT(*)
    ),
    2
  ) AS error_rate_pct
FROM
  `PROJECT_ID.meridian_log_bucket_analytics._AllLogs`
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
  AND resource.type = 'k8s_container'
  AND jsonPayload.service IN ('storefront-ui', 'order-service')
GROUP BY
  time_bucket, service_name, cluster_name, release_track
ORDER BY
  time_bucket ASC;


-- ----------------------------------------------------------------------------
-- PANEL 2: Pod Restart Counts by Namespace & Cluster (GCP Log Bucket / SIEM)
-- ----------------------------------------------------------------------------
SELECT
  COALESCE(resource.labels.namespace_name, jsonPayload.namespace, 'meridian-prod') AS namespace_name,
  COALESCE(resource.labels.cluster_name, jsonPayload.cluster_name) AS cluster_name,
  COALESCE(jsonPayload.service, resource.labels.container_name) AS workload,
  COUNTIF(
    jsonPayload.reason IN ('BackOff', 'OOMKilled', 'ContainerCannotRun', 'Error')
    OR textPayload LIKE '%Back-off restarting failed container%'
    OR jsonPayload.event_type = 'POD_RESTART'
  ) AS restart_count,
  MAX(timestamp) AS last_restart_timestamp
FROM
  `PROJECT_ID.meridian_log_bucket_analytics._AllLogs`
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
GROUP BY
  namespace_name, cluster_name, workload
HAVING
  restart_count > 0
ORDER BY
  restart_count DESC;


-- ----------------------------------------------------------------------------
-- PANEL 3: Request Latency Percentiles (p50, p95, p99) Across Stable & Canary
-- ----------------------------------------------------------------------------
SELECT
  TIMESTAMP_TRUNC(timestamp, MINUTE) AS time_bucket,
  jsonPayload.service AS service_name,
  jsonPayload.release_track AS release_track,
  APPROX_QUANTILES(CAST(jsonPayload.latency_ms AS FLOAT64), 100)[OFFSET(50)] AS p50_latency_ms,
  APPROX_QUANTILES(CAST(jsonPayload.latency_ms AS FLOAT64), 100)[OFFSET(95)] AS p95_latency_ms,
  APPROX_QUANTILES(CAST(jsonPayload.latency_ms AS FLOAT64), 100)[OFFSET(99)] AS p99_latency_ms
FROM
  `PROJECT_ID.meridian_log_bucket_analytics._AllLogs`
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
  AND resource.type = 'k8s_container'
  AND jsonPayload.latency_ms IS NOT NULL
GROUP BY
  time_bucket, service_name, release_track
ORDER BY
  time_bucket ASC;


-- ----------------------------------------------------------------------------
-- PANEL 4: Resource Utilization Trends (CPU % & Memory MB During Traffic Flip)
-- ----------------------------------------------------------------------------
SELECT
  TIMESTAMP_TRUNC(timestamp, MINUTE) AS time_bucket,
  jsonPayload.cluster_name AS cluster_name,
  jsonPayload.service AS service_name,
  ROUND(AVG(CAST(jsonPayload.cpu_utilization_pct AS FLOAT64)), 2) AS avg_cpu_pct,
  ROUND(MAX(CAST(jsonPayload.cpu_utilization_pct AS FLOAT64)), 2) AS peak_cpu_pct,
  ROUND(AVG(CAST(jsonPayload.memory_used_mb AS FLOAT64)), 1) AS avg_memory_mb
FROM
  `PROJECT_ID.meridian_log_bucket_analytics._AllLogs`
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
  AND resource.type = 'k8s_container'
  AND jsonPayload.cpu_utilization_pct IS NOT NULL
GROUP BY
  time_bucket, cluster_name, service_name
ORDER BY
  time_bucket ASC;


-- ----------------------------------------------------------------------------
-- SIEM & SECURITY AUDIT QUERY 5: Akamai True-Client-IP + NGINX mTLS Audit Trail
-- Correlates Akamai Edge Client IPs, mTLS Client Certificate DNs, and Cloud Traces
-- ----------------------------------------------------------------------------
SELECT
  timestamp,
  jsonPayload.true_client_ip AS akamai_true_client_ip,
  jsonPayload.akamai_request_bc AS akamai_breadcrumb,
  jsonPayload.mtls_client_subject AS nginx_mtls_client_dn,
  jsonPayload.cluster_name AS served_by_cluster,
  jsonPayload.release_track AS release_track,
  jsonPayload.service AS service,
  CAST(jsonPayload.status_code AS INT64) AS http_status,
  CAST(jsonPayload.latency_ms AS FLOAT64) AS latency_ms,
  trace
FROM
  `PROJECT_ID.meridian_log_bucket_analytics._AllLogs`
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
  AND jsonPayload.true_client_ip IS NOT NULL
ORDER BY
  timestamp DESC
LIMIT 50;