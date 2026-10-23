-- ============================================================================
-- MERIDIAN COMMERCE PLATFORM: BIGQUERY QUERIES FOR INGESTED CLOUD LOG FILES
--
-- Before running this script, create two external tables in the BigQuery
-- console with schema auto-detection enabled:
--   cs-512516.logs_ingest.live_docker_ingested_logs
--     gs://<valid-bucket-name>/cs_cloud_logs/live_docker_ingested_logs.jsonl
--   cs-512516.logs_ingest.cloud_logs
--     gs://<valid-bucket-name>/cs_cloud_logs/cloud_logs.jsonl
-- Replace <valid-bucket-name> with the actual GCS bucket; bucket names cannot
-- contain underscores, so `mcp_log_ingestion-1` is not a valid bucket name.
-- Select NEWLINE_DELIMITED_JSON as the source format and enable Auto detect.
-- BigQuery SQL CREATE EXTERNAL TABLE does not accept an autodetect option.
-- The BigQuery service account must have read access to both GCS objects.
-- ============================================================================

-- Normalize either flat application logs or Cloud Logging-style jsonPayload
-- records so all panels can query both ingested sources consistently.
-- sample_cloud_logs.jsonl is a duplicate export of cloud_logs.jsonl and is
-- intentionally not unioned here to avoid double-counting simulated records.
-- Injected research scenarios also appear in the live capture; deduplicate
-- only those repeated scenario trace IDs while preserving distinct log events.
CREATE TEMP TABLE normalized_logs AS
WITH raw_logs AS (
  SELECT TO_JSON_STRING(log_record) AS record, 0 AS source_priority
  FROM `cs-512516.logs_ingest.cloud_logs` AS log_record
  UNION ALL
  SELECT TO_JSON_STRING(log_record) AS record, 1 AS source_priority
  FROM `cs-512516.logs_ingest.live_docker_ingested_logs` AS log_record
),
extracted_logs AS (
SELECT
  record,
  source_priority,
  SAFE_CAST(COALESCE(
    JSON_VALUE(record, '$.timestamp'),
    JSON_VALUE(record, '$.time')
  ) AS TIMESTAMP) AS log_timestamp,
  COALESCE(
    JSON_VALUE(record, '$.severity'),
    JSON_VALUE(record, '$.jsonPayload.severity')
  ) AS severity,
  COALESCE(
    JSON_VALUE(record, '$.service'),
    JSON_VALUE(record, '$.jsonPayload.service'),
    JSON_VALUE(record, '$.resource.labels.container_name')
  ) AS service_name,
  COALESCE(
    JSON_VALUE(record, '$.cluster_name'),
    JSON_VALUE(record, '$.jsonPayload.cluster_name'),
    JSON_VALUE(record, '$.resource.labels.cluster_name')
  ) AS cluster_name,
  COALESCE(
    JSON_VALUE(record, '$.release_track'),
    JSON_VALUE(record, '$.jsonPayload.release_track')
  ) AS release_track,
  COALESCE(
    SAFE_CAST(JSON_VALUE(record, '$.status_code') AS INT64),
    SAFE_CAST(JSON_VALUE(record, '$.jsonPayload.status_code') AS INT64),
    SAFE_CAST(JSON_VALUE(record, '$.httpRequest.status') AS INT64)
  ) AS status_code,
  COALESCE(
    SAFE_CAST(JSON_VALUE(record, '$.latency_ms') AS FLOAT64),
    SAFE_CAST(JSON_VALUE(record, '$.jsonPayload.latency_ms') AS FLOAT64)
  ) AS latency_ms,
  COALESCE(
    SAFE_CAST(JSON_VALUE(record, '$.cpu_utilization_pct') AS FLOAT64),
    SAFE_CAST(JSON_VALUE(record, '$.jsonPayload.cpu_utilization_pct') AS FLOAT64)
  ) AS cpu_utilization_pct,
  COALESCE(
    SAFE_CAST(JSON_VALUE(record, '$.memory_used_mb') AS FLOAT64),
    SAFE_CAST(JSON_VALUE(record, '$.jsonPayload.memory_used_mb') AS FLOAT64)
  ) AS memory_used_mb,
  COALESCE(
    JSON_VALUE(record, '$.namespace'),
    JSON_VALUE(record, '$.jsonPayload.namespace'),
    JSON_VALUE(record, '$.resource.labels.namespace_name'),
    'meridian-prod'
  ) AS namespace_name,
  COALESCE(
    JSON_VALUE(record, '$.reason'),
    JSON_VALUE(record, '$.jsonPayload.reason')
  ) AS reason,
  COALESCE(
    JSON_VALUE(record, '$.event_type'),
    JSON_VALUE(record, '$.jsonPayload.event_type')
  ) AS event_type,
  COALESCE(
    JSON_VALUE(record, '$.textPayload'),
    JSON_VALUE(record, '$.text_payload')
  ) AS text_payload,
  COALESCE(
    JSON_VALUE(record, '$.true_client_ip'),
    JSON_VALUE(record, '$.jsonPayload.true_client_ip')
  ) AS true_client_ip,
  COALESCE(
    JSON_VALUE(record, '$.akamai_request_bc'),
    JSON_VALUE(record, '$.jsonPayload.akamai_request_bc')
  ) AS akamai_request_bc,
  COALESCE(
    JSON_VALUE(record, '$.mtls_client_subject'),
    JSON_VALUE(record, '$.jsonPayload.mtls_client_subject')
  ) AS mtls_client_subject,
  COALESCE(
    JSON_VALUE(record, '$.trace'),
    JSON_VALUE(record, '$.log_gapis_trace'),
    JSON_VALUE(record, '$.trace_id'),
    JSON_VALUE(record, '$.jsonPayload.trace')
  ) AS trace_id,
  COALESCE(
    JSON_VALUE(record, '$.error_scenario'),
    JSON_VALUE(record, '$.jsonPayload.error_scenario')
  ) AS error_scenario,
  COALESCE(
    JSON_VALUE(record, '$.message'),
    JSON_VALUE(record, '$.jsonPayload.message')
  ) AS message,
  COALESCE(
    JSON_VALUE(record, '$.pod_name'),
    JSON_VALUE(record, '$.jsonPayload.pod_name'),
    JSON_VALUE(record, '$.resource.labels.pod_name')
  ) AS pod_name
FROM raw_logs
),
normalized_logs AS (
  SELECT * EXCEPT (record, source_priority)
  FROM extracted_logs
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY IF(
      error_scenario = 'injected_container_error',
      trace_id,
      record
    )
    ORDER BY source_priority
  ) = 1
)
SELECT * FROM normalized_logs;

-- ----------------------------------------------------------------------------
-- PANEL 1: Application Error Rates Over Time
-- ----------------------------------------------------------------------------
SELECT
  TIMESTAMP_TRUNC(log_timestamp, MINUTE) AS time_bucket,
  service_name,
  cluster_name,
  release_track,
  COUNT(*) AS total_requests,
  COUNTIF(severity = 'ERROR' OR status_code >= 500) AS error_count,
  ROUND(
    SAFE_DIVIDE(
      COUNTIF(severity = 'ERROR' OR status_code >= 500) * 100.0,
      COUNT(*)
    ),
    2
  ) AS error_rate_pct
FROM normalized_logs
WHERE
  log_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
  AND service_name IN ('storefront-ui', 'order-service')
GROUP BY
  time_bucket, service_name, cluster_name, release_track
ORDER BY
  time_bucket ASC;


-- ----------------------------------------------------------------------------
-- PANEL 2: Pod Restart Counts by Namespace & Cluster
-- ----------------------------------------------------------------------------
SELECT
  namespace_name,
  cluster_name,
  COALESCE(service_name, 'unknown') AS workload,
  COUNTIF(
    reason IN ('BackOff', 'OOMKilled', 'ContainerCannotRun', 'Error')
    OR text_payload LIKE '%Back-off restarting failed container%'
    OR event_type = 'POD_RESTART'
  ) AS restart_count,
  MAX(log_timestamp) AS last_restart_timestamp
FROM normalized_logs
WHERE
  log_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
GROUP BY
  namespace_name, cluster_name, workload
HAVING
  restart_count > 0
ORDER BY
  restart_count DESC;


-- ----------------------------------------------------------------------------
-- PANEL 3: Request Latency Percentiles (p50, p95, p99)
-- ----------------------------------------------------------------------------
SELECT
  TIMESTAMP_TRUNC(log_timestamp, MINUTE) AS time_bucket,
  service_name,
  release_track,
  APPROX_QUANTILES(latency_ms, 100)[OFFSET(50)] AS p50_latency_ms,
  APPROX_QUANTILES(latency_ms, 100)[OFFSET(95)] AS p95_latency_ms,
  APPROX_QUANTILES(latency_ms, 100)[OFFSET(99)] AS p99_latency_ms
FROM normalized_logs
WHERE
  log_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
  AND latency_ms IS NOT NULL
GROUP BY
  time_bucket, service_name, release_track
ORDER BY
  time_bucket ASC;


-- ----------------------------------------------------------------------------
-- PANEL 4: Resource Utilization Trends (CPU % & Memory MB)
-- ----------------------------------------------------------------------------
SELECT
  TIMESTAMP_TRUNC(log_timestamp, MINUTE) AS time_bucket,
  cluster_name,
  service_name,
  ROUND(AVG(cpu_utilization_pct), 2) AS avg_cpu_pct,
  ROUND(MAX(cpu_utilization_pct), 2) AS peak_cpu_pct,
  ROUND(AVG(memory_used_mb), 1) AS avg_memory_mb
FROM normalized_logs
WHERE
  log_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
  AND cpu_utilization_pct IS NOT NULL
GROUP BY
  time_bucket, cluster_name, service_name
ORDER BY
  time_bucket ASC;


-- ----------------------------------------------------------------------------
-- SIEM & SECURITY AUDIT QUERY 5: Akamai True-Client-IP + NGINX mTLS Audit Trail
-- ----------------------------------------------------------------------------
SELECT
  log_timestamp AS timestamp,
  true_client_ip AS akamai_true_client_ip,
  akamai_request_bc AS akamai_breadcrumb,
  mtls_client_subject AS nginx_mtls_client_dn,
  cluster_name AS served_by_cluster,
  release_track,
  service_name AS service,
  status_code AS http_status,
  latency_ms,
  trace_id
FROM normalized_logs
WHERE
  log_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 6 HOUR)
  AND true_client_ip IS NOT NULL
ORDER BY
  log_timestamp DESC
LIMIT 50;


-- ----------------------------------------------------------------------------
-- RESEARCH: HTTP Errors with Trace IDs for Log / Trace Explorer
-- Includes deterministic injected scenarios and errors from captured app logs.
-- ----------------------------------------------------------------------------
SELECT
  log_timestamp AS timestamp,
  cluster_name,
  service_name,
  pod_name,
  status_code AS http_status,
  reason,
  error_scenario,
  message,
  trace_id
FROM normalized_logs
WHERE
  log_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
  AND (severity = 'ERROR' OR status_code >= 500)
ORDER BY
  log_timestamp DESC
LIMIT 100;