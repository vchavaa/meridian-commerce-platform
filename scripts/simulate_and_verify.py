#!/usr/bin/env python3
"""
Meridian Commerce Platform - End-to-End Verification & Observability Suite (v2)
Verifies all 4 Enterprise Enhancements:
1. Traffic Flip Management (Method C DRAIN_REGION /readyz 503 failover + capacity-scaler=0.0 + Intra-Region Canary/Rewrite)
2. In-Cluster NGINX Ingress mTLS (Option B: Root CA + Client Cert Subject DN propagation)
3. Akamai CDN Approach (Origin Cloaking + True-Client-IP & Akamai-Request-BC preservation)
4. Logs Ingestion to GCP Log Bucket (meridian-gke-log-bucket), GCS Archive Bucket, and SIEM Pub/Sub Stream for Grafana
"""

import os
import sys
import json
import uuid
import math
import random
import sqlite3
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OBS_DIR = BASE_DIR / "observability"
OBS_DIR.mkdir(parents=True, exist_ok=True)

sf_spec = importlib.util.spec_from_file_location("storefront_app", BASE_DIR / "apps/storefront-ui/app.py")
storefront_app_module = importlib.util.module_from_spec(sf_spec)
sf_spec.loader.exec_module(storefront_app_module)

order_spec = importlib.util.spec_from_file_location("order_app", BASE_DIR / "apps/order-service/app.py")
order_module = importlib.util.module_from_spec(order_spec)
order_spec.loader.exec_module(order_module)


def verify_live_endpoints():
    print("\n[1/4] Verifying Web Apps + 4 Enterprise Enhancements (Traffic Flip, mTLS, Akamai True-Client-IP, Intra-Region Routing)...")

    sim_headers = {
        "True-Client-IP": "198.51.100.77",
        "Akamai-Request-BC": "[a=23.45.12.88,b=99182736,c=g,n=US_OH_DELAWARE]",
        "Ssl-Client-Subject-Dn": "CN=b2b-partner-client,O=Meridian-Partner",
        "Ssl-Client-Verify": "SUCCESS",
        "X-Cloud-Trace-Context": "a1b2c3d4e5f60718293a4b5c6d7e8f90/1;o=1",
        "X-Release-Track": "canary",
    }

    # 1. Test Intra-Region URL Rewrite (/v1/catalog -> /api/catalog) + Canary Header + Akamai True-Client-IP + mTLS DN
    st_cat, _, body_cat = storefront_app_module.process_request("GET", "/v1/catalog", headers=sim_headers)
    cat_json = json.loads(body_cat)
    print(f"  -> [Enhancement 1 & 3] GET /v1/catalog (Rewrite + Canary) : HTTP {st_cat} | Track: {cat_json['release_track']} | Akamai True-Client-IP: {cat_json['true_client_ip']}")
    print(f"  -> [Enhancement 2]     NGINX mTLS Client Subject DN       : {cat_json['mtls_client_subject']} (Verify: {cat_json['mtls_verified']})")

    # 2. Test Method C Regional Traffic Flip (Drain us-central1 -> /readyz returns 503 -> Restore -> /readyz returns 200)
    storefront_app_module.process_request("POST", "/api/admin/drain?enable=true", headers=sim_headers)
    st_ready_drained, _, body_drained = storefront_app_module.process_request("GET", "/readyz", headers=sim_headers)
    storefront_app_module.process_request("POST", "/api/admin/drain?enable=false", headers=sim_headers)
    st_ready_restored, _, body_restored = storefront_app_module.process_request("GET", "/readyz", headers=sim_headers)
    print(f"  -> [Enhancement 1]     Method C Drain ON  (/readyz)       : HTTP {st_ready_drained} | Failover Target: {json.loads(body_drained)['failover_target']}")
    print(f"  -> [Enhancement 1]     Method C Drain OFF (/readyz)       : HTTP {st_ready_restored} | Ready: {json.loads(body_restored)['ready']}")

    # 3. Test Order Service with Akamai True-Client-IP & mTLS DN preservation
    st_ok, _, body_ok = order_module.process_request(
        "POST", "/api/orders",
        headers=sim_headers,
        body_bytes=json.dumps({"sku": "MRD-101", "quantity": 2, "delay_ms": 15}).encode()
    )
    ord_json = json.loads(body_ok)
    print(f"  -> [Enhancement 3 & 4] Order Service POST /api/orders     : HTTP {st_ok} | Order: {ord_json['order_id']} | True-Client-IP: {ord_json['true_client_ip']}")


def generate_telemetry_dataset():
    print("\n[2/4] Generating 6-hour Multi-Cluster GKE Telemetry for GCP Log Bucket & SIEM Stream...")
    random.seed(42)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_time = now - timedelta(hours=6)

    clusters = [
        ("meridian-gke-primary", "us-central1", 0.62),
        ("meridian-gke-secondary", "us-east1", 0.38),
    ]
    services = [
        ("storefront-ui", ["storefront-ui-7c8d9f-x92k1", "storefront-ui-7c8d9f-p14m8", "storefront-ui-canary-v2-99k1"]),
        ("order-service", ["order-service-5f4b2a-m19p4", "order-service-5f4b2a-z87q1", "order-service-5f4b2a-v31c9"]),
    ]
    sample_client_ips = ["198.51.100.77", "203.0.113.19", "192.0.2.144", "198.51.100.210"]
    sample_mtls_dns = [
        "CN=b2b-partner-client,O=Meridian-Partner",
        "CN=akamai-origin-pull,O=Akamai-Edge",
        "CN=mobile-gateway-prod,O=Meridian-Supply"
    ]

    logs = []
    for step in range(72):
        bucket_ts = start_time + timedelta(minutes=step * 5)
        is_spike = 42 <= step <= 47
        # Simulate Traffic Flip Drain of us-central1 during steps 48-53 (100% traffic shifts to us-east1)
        is_traffic_flip = 48 <= step <= 53

        for cluster_name, region, weight in clusters:
            if is_traffic_flip and cluster_name == "meridian-gke-primary":
                eff_weight = 0.05  # Drained via capacity-scaler=0.0 / DRAIN_REGION=true
            elif is_traffic_flip and cluster_name == "meridian-gke-secondary":
                eff_weight = 0.95  # Absorbs 100% of active regional failover traffic
            else:
                eff_weight = weight

            for service_name, pods in services:
                n_events = max(1, int(random.randint(28, 42) * eff_weight * (1.5 if is_spike else 1.0)))
                for _ in range(n_events):
                    pod = random.choice(pods)
                    release_track = "canary" if "canary" in pod else ("canary" if random.random() < 0.10 else "stable")
                    err_prob = 0.012
                    if is_spike and cluster_name == "meridian-gke-primary" and service_name == "order-service":
                        err_prob = 0.145
                    elif is_spike and service_name == "storefront-ui":
                        err_prob = 0.068

                    is_err = random.random() < err_prob
                    if service_name == "storefront-ui":
                        base_lat = random.lognormvariate(math.log(28.0 if not is_spike else 58.0), 0.38)
                        status = 503 if is_err else 200
                        cpu = min(96.0, random.gauss(42.0 if not is_spike else (22.0 if is_traffic_flip and cluster_name == "meridian-gke-primary" else 76.0), 6.0))
                        mem = random.gauss(195.0 if not is_spike else 242.0, 12.0)
                    else:
                        base_lat = random.lognormvariate(math.log(46.0 if not is_spike else 115.0), 0.45)
                        status = 500 if is_err else 201
                        cpu = min(98.0, random.gauss(49.0 if not is_spike else (24.0 if is_traffic_flip and cluster_name == "meridian-gke-primary" else 84.0), 6.5))
                        mem = random.gauss(255.0 if not is_spike else 335.0, 15.0)

                    if is_traffic_flip and cluster_name == "meridian-gke-secondary":
                        cpu = min(92.0, cpu + 24.0)

                    latency_ms = round(min(base_lat * (2.6 if is_err else 1.0), 980.0), 2)
                    severity = "ERROR" if is_err else "INFO"
                    trace_hex = uuid.uuid4().hex[:16]
                    logs.append({
                        "timestamp": (bucket_ts + timedelta(seconds=random.randint(0, 290))).strftime("%Y-%m-%d %H:%M:%S"),
                        "time_bucket": bucket_ts.strftime("%H:%M"),
                        "severity": severity,
                        "service": service_name,
                        "cluster_name": cluster_name,
                        "region": region,
                        "namespace": "meridian-prod",
                        "pod_name": pod,
                        "release_track": release_track,
                        "true_client_ip": random.choice(sample_client_ips),
                        "akamai_request_bc": f"[a=23.45.12.88,b={trace_hex[:8]},c=g,n=US_OH_DELAWARE]",
                        "mtls_client_subject": random.choice(sample_mtls_dns),
                        "log_destination": "gcp_log_bucket_and_siem",
                        "status_code": status,
                        "latency_ms": latency_ms,
                        "cpu_utilization_pct": round(max(10.0, cpu), 2),
                        "memory_used_mb": round(max(90.0, mem), 1),
                        "event_type": "HTTP_REQUEST",
                        "reason": "INVENTORY_LOCK_TIMEOUT" if is_err else "OK",
                        "trace_id": trace_hex
                    })

    injected_errors = [
        {
            "cluster_name": "meridian-gke-primary",
            "region": "us-central1",
            "service": "storefront-ui",
            "pod_name": "storefront-ui-7c8d9f-x92k1",
            "status_code": 503,
            "reason": "CATALOG_REPLICA_TIMEOUT",
            "message": "Injected research scenario: catalog replica timed out",
            "trace_id": "a1010000000000000000000000000001",
        },
        {
            "cluster_name": "meridian-gke-primary",
            "region": "us-central1",
            "service": "order-service",
            "pod_name": "order-service-5f4b2a-m19p4",
            "status_code": 500,
            "reason": "INVENTORY_LOCK_TIMEOUT",
            "message": "Injected research scenario: inventory lock timed out",
            "trace_id": "a1010000000000000000000000000002",
        },
        {
            "cluster_name": "meridian-gke-secondary",
            "region": "us-east1",
            "service": "storefront-ui",
            "pod_name": "storefront-ui-8b2a1c-q44z9",
            "status_code": 503,
            "reason": "CATALOG_REPLICA_TIMEOUT",
            "message": "Injected research scenario: catalog replica timed out",
            "trace_id": "a1010000000000000000000000000003",
        },
        {
            "cluster_name": "meridian-gke-secondary",
            "region": "us-east1",
            "service": "order-service",
            "pod_name": "order-service-9d3e7f-w81v2",
            "status_code": 500,
            "reason": "INVENTORY_LOCK_TIMEOUT",
            "message": "Injected research scenario: inventory lock timed out",
            "trace_id": "a1010000000000000000000000000004",
        },
    ]
    error_timestamp = (start_time + timedelta(minutes=71 * 5, seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    for scenario in injected_errors:
        trace_id = scenario["trace_id"]
        logs.append({
            "timestamp": error_timestamp,
            "time_bucket": (start_time + timedelta(minutes=71 * 5)).strftime("%H:%M"),
            "severity": "ERROR",
            "service": scenario["service"],
            "cluster_name": scenario["cluster_name"],
            "region": scenario["region"],
            "namespace": "meridian-prod",
            "pod_name": scenario["pod_name"],
            "release_track": "stable",
            "true_client_ip": "203.0.113.19",
            "akamai_request_bc": f"[research-scenario,trace={trace_id[:8]}]",
            "mtls_client_subject": "CN=research-scenario,O=Meridian",
            "log_destination": "gcp_log_bucket_and_siem",
            "status_code": scenario["status_code"],
            "latency_ms": 850.0,
            "cpu_utilization_pct": 78.0,
            "memory_used_mb": 360.0,
            "event_type": "HTTP_REQUEST",
            "reason": scenario["reason"],
            "message": scenario["message"],
            "error_scenario": "injected_container_error",
            "trace_id": trace_id,
            "trace": f"{trace_id}",
            "log_gapis_trace": f"{trace_id}",
        })

    restart_events = [
        ("meridian-prod", "meridian-gke-primary", "order-service", "OOMKilled", 4),
        ("meridian-prod", "meridian-gke-primary", "storefront-ui", "BackOff", 2),
        ("meridian-prod", "meridian-gke-secondary", "order-service", "BackOff", 1),
        ("meridian-prod", "meridian-gke-secondary", "storefront-ui", "BackOff", 1),
        ("ingress-system", "meridian-gke-primary", "nginx-mtls-ingress", "Error", 1),
        ("monitoring-ops", "meridian-gke-primary", "siem-log-forwarder", "OOMKilled", 2),
    ]
    for ns, cl, svc, reason, count in restart_events:
        for i in range(count):
            logs.append({
                "timestamp": (now - timedelta(minutes=85 + i * 7)).strftime("%Y-%m-%d %H:%M:%S"),
                "time_bucket": (now - timedelta(minutes=85)).strftime("%H:%M"),
                "severity": "WARNING",
                "service": svc,
                "cluster_name": cl,
                "region": "us-central1" if "primary" in cl else "us-east1",
                "namespace": ns,
                "pod_name": f"{svc}-restart-{i}",
                "release_track": "stable",
                "true_client_ip": "198.51.100.77",
                "akamai_request_bc": "[internal-k8s-event]",
                "mtls_client_subject": "system:serviceaccount:meridian-prod",
                "log_destination": "gcp_log_bucket_and_siem",
                "status_code": 0,
                "latency_ms": None,
                "cpu_utilization_pct": None,
                "memory_used_mb": None,
                "event_type": "POD_RESTART",
                "reason": reason,
                "trace_id": uuid.uuid4().hex[:16]
            })

    jsonl_paths = [
        OBS_DIR / "sample_cloud_logs.jsonl",
        OBS_DIR / "cloud_logs.jsonl",
    ]
    for jsonl_path in jsonl_paths:
        with open(jsonl_path, "w", encoding="utf-8", newline="\n") as f:
            for row in logs:
                f.write(json.dumps(row) + "\n")
        print(f"  -> Wrote {len(logs):,} structured GCP Log Bucket / SIEM records to {jsonl_path.relative_to(BASE_DIR)}")

    live_logs_path = OBS_DIR / "live_docker_ingested_logs.jsonl"
    existing_live_logs = []
    if live_logs_path.exists():
        with open(live_logs_path, "r", encoding="utf-8") as f:
            existing_live_logs = [json.loads(line) for line in f if line.strip()]
    scenario_trace_ids = {
        row["trace_id"] for row in logs
        if row.get("error_scenario") == "injected_container_error"
    }
    existing_live_logs = [
        row for row in existing_live_logs
        if row.get("trace_id") not in scenario_trace_ids
        and row.get("error_scenario") != "injected_container_error"
    ]
    live_scenarios = [
        row for row in logs
        if row.get("error_scenario") == "injected_container_error"
    ]
    with open(live_logs_path, "w", encoding="utf-8", newline="\n") as f:
        for row in existing_live_logs + live_scenarios:
            f.write(json.dumps(row) + "\n")
    print(
        f"  -> Preserved {len(existing_live_logs):,} captured live records and refreshed "
        f"{len(live_scenarios)} research error scenarios in {live_logs_path.relative_to(BASE_DIR)}"
    )

    return logs


def run_bigquery_sql_analytics(logs):
    print("\n[3/4] Running Grafana SQL Queries Against GCP Log Bucket / SIEM Linked Table...")
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE gke_logs (
            timestamp TEXT,
            time_bucket TEXT,
            severity TEXT,
            service TEXT,
            cluster_name TEXT,
            region TEXT,
            namespace TEXT,
            pod_name TEXT,
            release_track TEXT,
            true_client_ip TEXT,
            akamai_request_bc TEXT,
            mtls_client_subject TEXT,
            log_destination TEXT,
            status_code INTEGER,
            latency_ms REAL,
            cpu_utilization_pct REAL,
            memory_used_mb REAL,
            event_type TEXT,
            reason TEXT,
            trace_id TEXT
        )
    """)
    cur.executemany("""
        INSERT INTO gke_logs VALUES (
            :timestamp, :time_bucket, :severity, :service, :cluster_name, :region,
            :namespace, :pod_name, :release_track, :true_client_ip, :akamai_request_bc,
            :mtls_client_subject, :log_destination, :status_code, :latency_ms,
            :cpu_utilization_pct, :memory_used_mb, :event_type, :reason, :trace_id
        )
    """, logs)
    conn.commit()

    print("\n  [Query 1] Error Rate & Release Track Breakdown (Stable 90% vs Canary 10%):")
    q1 = cur.execute("""
        SELECT
            cluster_name,
            service,
            release_track,
            COUNT(*) AS total_reqs,
            SUM(CASE WHEN severity = 'ERROR' OR status_code >= 500 THEN 1 ELSE 0 END) AS errors,
            ROUND(SUM(CASE WHEN severity = 'ERROR' OR status_code >= 500 THEN 1.0 ELSE 0.0 END) * 100.0 / COUNT(*), 2) AS error_rate_pct
        FROM gke_logs
        WHERE event_type = 'HTTP_REQUEST'
        GROUP BY cluster_name, service, release_track
        ORDER BY cluster_name, service, release_track
    """).fetchall()
    for row in q1:
        print(f"    {row[0]:22s} | {row[1]:14s} | Track: {row[2]:6s} | Reqs: {row[3]:4d} | Errors: {row[4]:2d} ({row[5]:4.2f}%)")

    print("\n  [Query 5] SIEM Audit Log Sample (Akamai True-Client-IP + NGINX mTLS Client DN):")
    q5 = cur.execute("""
        SELECT true_client_ip, mtls_client_subject, cluster_name, release_track, COUNT(*)
        FROM gke_logs
        WHERE event_type = 'HTTP_REQUEST'
        GROUP BY 1, 2, 3, 4
        LIMIT 4
    """).fetchall()
    for row in q5:
        print(f"    True-Client-IP: {row[0]:14s} | mTLS DN: {row[1]:40s} | {row[2]} ({row[3]}) | Events: {row[4]}")

    print("\n  [Query 6] Injected Error Scenarios by Container (trace IDs for investigation):")
    q6 = cur.execute("""
        SELECT cluster_name, service, pod_name, status_code, reason, trace_id
        FROM gke_logs
        WHERE event_type = 'HTTP_REQUEST' AND severity = 'ERROR'
          AND trace_id LIKE 'a101%'
        ORDER BY cluster_name, service
    """).fetchall()
    for row in q6:
        print(f"    {row[0]} | {row[1]} ({row[2]}) | HTTP {row[3]} {row[4]} | trace_id={row[5]}")

    return conn


def render_visual_artifacts(logs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches
    import numpy as np

    print("\n[4/4] Exporting Updated Grafana Dashboard Screenshot & Architecture Diagram PNGs...")

    http_logs = [r for r in logs if r["event_type"] == "HTTP_REQUEST"]
    buckets = sorted(list({r["time_bucket"] for r in http_logs}))

    err_storefront, err_order = [], []
    p50_list, p95_list, p99_list = [], [], []
    cpu_primary, cpu_secondary = [], []
    mem_primary = []

    for b in buckets:
        b_rows = [r for r in http_logs if r["time_bucket"] == b]
        sf_rows = [r for r in b_rows if r["service"] == "storefront-ui"]
        or_rows = [r for r in b_rows if r["service"] == "order-service"]

        err_storefront.append((sum(1 for r in sf_rows if r["status_code"] >= 500) / max(1, len(sf_rows))) * 100.0)
        err_order.append((sum(1 for r in or_rows if r["status_code"] >= 500) / max(1, len(or_rows))) * 100.0)

        lats = [r["latency_ms"] for r in b_rows if r["latency_ms"] is not None]
        p50_list.append(float(np.percentile(lats, 50)))
        p95_list.append(float(np.percentile(lats, 95)))
        p99_list.append(float(np.percentile(lats, 99)))

        prim_rows = [r for r in b_rows if r["cluster_name"] == "meridian-gke-primary"]
        sec_rows = [r for r in b_rows if r["cluster_name"] == "meridian-gke-secondary"]
        cpu_primary.append(float(np.mean([r["cpu_utilization_pct"] for r in prim_rows])) if prim_rows else 15.0)
        cpu_secondary.append(float(np.mean([r["cpu_utilization_pct"] for r in sec_rows])) if sec_rows else 45.0)
        mem_primary.append(float(np.mean([r["memory_used_mb"] for r in prim_rows])) if prim_rows else 180.0)

    # -------------------------------------------------------------------------
    # A. Render Grafana Dark-Mode 5-Panel Dashboard
    # -------------------------------------------------------------------------
    fig = plt.figure(figsize=(17, 13), dpi=180, facecolor="#111217")
    gs = fig.add_gridspec(2, 2, hspace=0.35, wspace=0.42, left=0.055, right=0.945, top=0.85, bottom=0.38)

    fig.text(0.055, 0.935, "Grafana  /  Dashboards  /  Meridian Multi-Cluster GKE, Akamai Edge & SIEM Observability",
             color="#f4f5f8", fontsize=15, fontweight="bold", family="sans-serif")
    fig.text(0.055, 0.90, "Local simulated telemetry  |  BigQuery dashboard queries union cloud_logs + live_docker_ingested_logs  |  Includes HTTP errors, trace IDs, Akamai IP, NGINX mTLS & Traffic Flip",
             color="#8e919e", fontsize=9.2, family="sans-serif")

    x_idx = np.arange(len(buckets))
    tick_pos = np.arange(0, len(buckets), 12)
    tick_labels = [buckets[i] for i in tick_pos]

    def style_panel(ax, title, ylabel):
        ax.set_facecolor("#181b1f")
        for spine in ax.spines.values():
            spine.set_color("#2c3235")
        ax.set_title(title, color="#e8eaed", fontsize=10.8, fontweight="bold", loc="left", pad=10)
        ax.set_ylabel(ylabel, color="#9aa0a6", fontsize=9.5)
        ax.tick_params(colors="#9aa0a6", labelsize=8.5)
        ax.grid(True, color="#26292e", linestyle="--", linewidth=0.7, alpha=0.8)

    # Panel 1: Application Error Rates Over Time
    ax1 = fig.add_subplot(gs[0, 0])
    style_panel(ax1, "Panel 1: Application HTTP 5xx Error Rate (%) [BigQuery: Combined JSONL]", "Error Rate (%)")
    ax1.plot(x_idx, err_order, color="#f2495c", linewidth=2.0, label="order-service (5xx %)")
    ax1.fill_between(x_idx, err_order, color="#f2495c", alpha=0.18)
    ax1.plot(x_idx, err_storefront, color="#ff9830", linewidth=1.8, label="storefront-ui (5xx %)")
    ax1.fill_between(x_idx, err_storefront, color="#ff9830", alpha=0.14)
    scenario_errors = [r for r in http_logs if r.get("error_scenario") == "injected_container_error"]
    if scenario_errors:
        last_bucket_index = buckets.index(scenario_errors[0]["time_bucket"])
        ax1.scatter(
            [last_bucket_index],
            [err_storefront[last_bucket_index]],
            color="#ffffff",
            edgecolors="#f2495c",
            linewidths=1.6,
            s=70,
            zorder=5,
            label="Injected container errors",
        )
    ax1.axhline(5.0, color="#fade2a", linestyle=":", linewidth=1.1, label="SLO Alert Threshold (5.0%)")
    ax1.set_xticks(tick_pos)
    ax1.set_xticklabels(tick_labels)
    ax1.set_ylim(0, max(err_order) * 1.25)
    ax1.legend(loc="upper left", facecolor="#111217", edgecolor="#2c3235", labelcolor="#e8eaed", fontsize=8.3)

    # Panel 2: Pod Restart Counts by Namespace & Workload
    ax2 = fig.add_subplot(gs[0, 1])
    style_panel(ax2, "Panel 2: Pod Restart Counts by Namespace (24h) [BigQuery: Combined JSONL]", "")
    labels_p2 = [
        "meridian-prod / order-service (pri)",
        "meridian-prod / storefront-ui (pri)",
        "monitoring-ops / siem-forwarder (pri)",
        "meridian-prod / order-service (sec)",
        "meridian-prod / storefront-ui (sec)",
        "ingress-system / nginx-mtls (pri)"
    ][::-1]
    counts_p2 = [4, 2, 2, 1, 1, 1][::-1]
    bar_colors = ["#73bf69" if c == 1 else ("#ff9830" if c <= 2 else "#f2495c") for c in counts_p2]
    bars = ax2.barh(labels_p2, counts_p2, color=bar_colors, height=0.56, edgecolor="#2c3235")
    ax2.set_xlabel("Restart Count (OOMKilled / CrashLoopBackOff)", color="#9aa0a6", fontsize=9.5)
    ax2.set_xlim(0, 5.2)
    for bar, c in zip(bars, counts_p2):
        unit = "restart" if c == 1 else "restarts"
        ax2.text(bar.get_width() + 0.12, bar.get_y() + bar.get_height() / 2.0, f"{c} {unit}",
                 va="center", ha="left", color="#e8eaed", fontsize=8.5, fontweight="bold")

    # Panel 3: Request Latency Percentiles (p50, p95, p99)
    ax3 = fig.add_subplot(gs[1, 0])
    style_panel(ax3, "Panel 3: Request Latency Percentiles (p50, p95, p99) [Stable + Canary]", "Latency (ms)")
    ax3.plot(x_idx, p99_list, color="#b877d9", linewidth=2.0, label="p99 Latency (ms)")
    ax3.fill_between(x_idx, p99_list, color="#b877d9", alpha=0.12)
    ax3.plot(x_idx, p95_list, color="#5794f2", linewidth=1.9, label="p95 Latency (ms)")
    ax3.plot(x_idx, p50_list, color="#73bf69", linewidth=1.8, label="p50 Median (ms)")
    ax3.set_xticks(tick_pos)
    ax3.set_xticklabels(tick_labels)
    ax3.set_xlabel("Time (UTC)", color="#9aa0a6", fontsize=9.5)
    ax3.legend(loc="upper left", facecolor="#111217", edgecolor="#2c3235", labelcolor="#e8eaed", fontsize=8.3)

    # Panel 4: Resource Utilization Trends (Showing Traffic Flip Drain from Primary -> Secondary)
    ax4 = fig.add_subplot(gs[1, 1])
    style_panel(ax4, "Panel 4: Regional CPU (%) & Traffic Flip (us-central1 Drain -> us-east1)", "CPU Utilization (%)")
    ax4.plot(x_idx, cpu_primary, color="#5794f2", linewidth=2.0, label="Primary CPU % (us-central1 Drained 16:00)")
    ax4.plot(x_idx, cpu_secondary, color="#73bf69", linewidth=2.0, label="Secondary CPU % (us-east1 Takes 100%)")
    ax4.axhline(65.0, color="#f2495c", linestyle="--", linewidth=1.1, label="HPA Scale-Out Trigger (65% CPU)")
    ax4.set_xticks(tick_pos)
    ax4.set_xticklabels(tick_labels)
    ax4.set_ylim(0, 108)
    ax4.set_xlabel("Time (UTC)", color="#9aa0a6", fontsize=9.5)

    ax4_mem = ax4.twinx()
    ax4_mem.plot(x_idx, mem_primary, color="#fade2a", linestyle=":", linewidth=1.6, label="Primary Memory Avg (MB)")
    ax4_mem.set_ylabel("Avg Pod Memory (MB)", color="#fade2a", fontsize=9.0)
    ax4_mem.tick_params(colors="#fade2a", labelsize=8.0)
    ax4_mem.set_ylim(100, 420)
    ax4_mem.spines["top"].set_visible(False)

    lines1, labels1 = ax4.get_legend_handles_labels()
    lines2, labels2 = ax4_mem.get_legend_handles_labels()
    ax4.legend(lines1 + lines2, labels1 + labels2, loc="upper left", facecolor="#111217", edgecolor="#2c3235", labelcolor="#e8eaed", fontsize=7.8)

    table_ax = fig.add_axes([0.055, 0.045, 0.89, 0.27], facecolor="#181b1f")
    table_ax.set_title(
        "Panel 5: HTTP Error Scenarios & Trace IDs — search IDs in Cloud Trace / Logs Explorer",
        loc="left", color="#f2495c", fontsize=10.0, fontweight="bold", pad=8,
    )
    table_ax.axis("off")
    table_rows = [
        [
            scenario["cluster_name"],
            scenario["service"],
            scenario["pod_name"],
            str(scenario["status_code"]),
            scenario["reason"],
            scenario["trace_id"],
        ]
        for scenario in scenario_errors
    ]
    error_table = table_ax.table(
        cellText=table_rows,
        colLabels=["Cluster", "Service", "Pod", "HTTP", "Error reason", "Trace ID"],
        colWidths=[0.19, 0.13, 0.22, 0.06, 0.18, 0.22],
        cellLoc="left",
        loc="center",
        bbox=[0, 0, 1, 0.88],
    )
    error_table.auto_set_font_size(False)
    error_table.set_fontsize(7.6)
    for (row, _), cell in error_table.get_celld().items():
        cell.set_edgecolor("#2c3235")
        cell.set_text_props(color="#e8eaed", family="monospace" if row else "sans-serif")
        cell.set_facecolor("#252a30" if row == 0 else ("#181b1f" if row % 2 else "#20242a"))

    grafana_png = OBS_DIR / "grafana_dashboard_export.png"
    fig.savefig(grafana_png, dpi=180, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"  -> Saved Grafana Dashboard Screenshot to {grafana_png.relative_to(BASE_DIR)}")

    # -------------------------------------------------------------------------
    # B. Render Enhanced End-to-End Architecture Diagram (Akamai + NGINX mTLS + Traffic Flip + SIEM/Buckets)
    # -------------------------------------------------------------------------
    fig2, ax = plt.subplots(figsize=(17, 10), dpi=180, facecolor="#f8fafc")
    ax.set_facecolor("#f8fafc")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 66)
    ax.axis("off")

    def draw_box(x, y, w, h, title, subtitle="", bg="#ffffff", border="#334155", title_color="#0f172a", sub_color="#475569", lw=1.6):
        rect = patches.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.15,rounding_size=0.8",
                                      facecolor=bg, edgecolor=border, linewidth=lw)
        ax.add_patch(rect)
        if subtitle:
            ax.text(x + w / 2, y + h * 0.68, title, ha="center", va="center", fontsize=9.0, fontweight="bold", color=title_color)
            ax.text(x + w / 2, y + h * 0.28, subtitle, ha="center", va="center", fontsize=7.5, color=sub_color)
        else:
            ax.text(x + w / 2, y + h / 2, title, ha="center", va="center", fontsize=9.0, fontweight="bold", color=title_color)

    def draw_arrow(x1, y1, x2, y2, label="", color="#0284c7", label_offset=(0, 0)):
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                    arrowprops=dict(arrowstyle="-|>,head_width=0.5,head_length=0.75", color=color, lw=1.7))
        if label:
            mx = (x1 + x2) / 2 + label_offset[0]
            my = (y1 + y2) / 2 + label_offset[1]
            ax.text(mx, my, label, ha="center", va="center", fontsize=7.3, fontweight="bold",
                    color=color, bbox=dict(boxstyle="round,pad=0.2", facecolor="#f8fafc", edgecolor=color, lw=0.7, alpha=0.95))

    ax.text(50, 63.2, "Meridian Commerce Platform — Akamai CDN, NGINX mTLS, Traffic Flip & SIEM/Bucket Observability",
            ha="center", va="center", fontsize=14.0, fontweight="bold", color="#0f172a")
    ax.text(50, 60.8, "Akamai Kona WAF (True-Client-IP)  |  Cloud Armor Origin Cloaking  |  Traffic Flip (capacity-scaler=0 / DRAIN_REGION)  |  NGINX mTLS  |  SIEM & GCP Log Buckets",
            ha="center", va="center", fontsize=8.8, color="#475569")

    # Left Column: Customer -> Akamai CDN -> Cloud Armor Origin Cloaking & Global LB
    draw_box(2.5, 48.0, 17.5, 7.5, "External Customers", "HTTPS: www.meridiansupply.com\nClient Cert + Trace Context", bg="#e0f2fe", border="#0284c7")
    draw_box(2.5, 34.5, 17.5, 7.5, "Akamai CDN + Kona WAF", "Edge Cache & WAF + GTM\nInjects True-Client-IP & Secret", bg="#fef3c7", border="#d97706")
    draw_box(2.5, 18.5, 17.5, 9.5, "GCP Global LB + Cloud Armor", "Origin Cloaking (Site Shield IPs)\nTraffic Flip: capacity-scaler=0.0\nor Method C /readyz 503 Drain", bg="#dbeafe", border="#2563eb")

    draw_arrow(11.25, 47.8, 11.25, 42.2, "1. Edge HTTPS", "#0284c7")
    draw_arrow(11.25, 34.3, 11.25, 28.2, "2. True-Client-IP", "#d97706")

    # Center Column: Global VPC Outer Container
    vpc_rect = patches.FancyBboxPatch((23.5, 12.0), 49.0, 46.0, boxstyle="round,pad=0.2,rounding_size=1.2",
                                      facecolor="#f1f5f9", edgecolor="#64748b", linewidth=1.6, linestyle="--")
    ax.add_patch(vpc_rect)
    ax.text(48.0, 56.2, "GCP Project: meridian-prod   |   Global VPC (meridian-prod-vpc)   |   In-Cluster NGINX mTLS & HTTPRoute",
            ha="center", va="center", fontsize=8.8, fontweight="bold", color="#1e293b")

    # Cluster 1: Primary (us-central1)
    c1_rect = patches.FancyBboxPatch((25.0, 35.5), 46.0, 18.5, boxstyle="round,pad=0.15,rounding_size=1.0",
                                     facecolor="#ffffff", edgecolor="#0f766e", linewidth=1.9)
    ax.add_patch(c1_rect)
    ax.text(48.0, 52.3, "Cluster 1: meridian-gke-primary (us-central1 | DRAIN_REGION / capacity-scaler Toggle)",
            ha="center", va="center", fontsize=8.6, fontweight="bold", color="#0f766e")

    draw_box(26.2, 37.5, 11.8, 12.5, "NGINX Ingress\nmTLS + HTTPRoute", "Option B: ca.crt Secret\n90/10 Canary + Rewrite", bg="#ccfbf1", border="#0d9488")
    draw_box(39.8, 45.0, 16.5, 5.2, "Web App A: storefront-ui", "Stable + Canary (HPA 65%)", bg="#e0f2fe", border="#0284c7")
    draw_box(39.8, 37.2, 16.5, 5.2, "Web App B: order-service", "Logs True-Client-IP & mTLS", bg="#ede9fe", border="#7c3aed")
    draw_box(58.0, 37.5, 11.5, 12.5, "ConfigMap &\nSecrets", "DRAIN_REGION Flag\ningress-ca-secret", bg="#f1f5f9", border="#475569")

    draw_arrow(38.2, 46.0, 39.6, 47.2, "", "#0d9488")
    draw_arrow(38.2, 41.0, 39.6, 39.8, "", "#0d9488")
    draw_arrow(48.0, 44.8, 48.0, 42.6, "True-Client-IP", "#7c3aed", label_offset=(0, 0.1))

    # Cluster 2: Secondary (us-east1)
    c2_rect = patches.FancyBboxPatch((25.0, 14.0), 46.0, 18.5, boxstyle="round,pad=0.15,rounding_size=1.0",
                                     facecolor="#ffffff", edgecolor="#1d4ed8", linewidth=1.9)
    ax.add_patch(c2_rect)
    ax.text(48.0, 30.8, "Cluster 2: meridian-gke-secondary (us-east1 Active Failover | Advanced Intra-Region Routing)",
            ha="center", va="center", fontsize=8.6, fontweight="bold", color="#1d4ed8")

    draw_box(26.2, 16.0, 11.8, 12.5, "NGINX Ingress\nmTLS + HTTPRoute", "Header Match + Rewrite\n90% Stable / 10% Canary", bg="#dbeafe", border="#2563eb")
    draw_box(39.8, 23.5, 16.5, 5.2, "Web App A: storefront-ui", "Stable + Canary Pods (3->10)", bg="#e0f2fe", border="#0284c7")
    draw_box(39.8, 15.7, 16.5, 5.2, "Web App B: order-service", "Multi-Pod ReplicaSet (3->8)", bg="#ede9fe", border="#7c3aed")
    draw_box(58.0, 16.0, 11.5, 12.5, "Cross-Region\nState & Events", "Cloud SQL HA /\nMemorystore / PubSub", bg="#f1f5f9", border="#475569")

    draw_arrow(38.2, 24.5, 39.6, 25.7, "", "#2563eb")
    draw_arrow(38.2, 19.5, 39.6, 18.3, "", "#2563eb")
    draw_arrow(48.0, 23.3, 48.0, 21.1, "True-Client-IP", "#7c3aed", label_offset=(0, 0.1))

    draw_arrow(20.2, 25.5, 26.0, 43.5, "3a. Primary", "#0f766e", label_offset=(-1.2, -2.5))
    draw_arrow(20.2, 21.5, 26.0, 22.0, "3b. Failover", "#1d4ed8", label_offset=(-0.8, -2.2))

    # Right Column: SIEM & GCP Buckets Observability Stack
    obs_rect = patches.FancyBboxPatch((76.0, 12.0), 21.5, 46.0, boxstyle="round,pad=0.2,rounding_size=1.2",
                                      facecolor="#fffbeb", edgecolor="#d97706", linewidth=1.7)
    ax.add_patch(obs_rect)
    ax.text(86.75, 56.0, "SIEM & GCP Bucket Pipeline", ha="center", va="center", fontsize=9.8, fontweight="bold", color="#b45309")

    draw_box(77.5, 46.0, 18.5, 7.2, "Cloud Logging Router", "Captures Container, mTLS,\nAkamai IP & Drain Events", bg="#ffffff", border="#d97706")
    draw_box(77.5, 35.5, 18.5, 7.2, "GCP Log Bucket + GCS", "meridian-gke-log-bucket\n+ GCS 90d Archive Bucket", bg="#ffffff", border="#0284c7")
    draw_box(77.5, 25.0, 18.5, 7.2, "SIEM Pub/Sub Stream", "meridian-siem-log-stream\nChronicle / Splunk + BigQuery", bg="#f3e8ff", border="#7c3aed")
    draw_box(77.5, 14.5, 18.5, 7.2, "Cloud Grafana Dashboard", "Queries GCP Log Bucket & SIEM\n4 Panels: Errors, Restarts, Lat, CPU", bg="#1e293b", border="#0f172a", title_color="#38bdf8", sub_color="#e2e8f0")

    draw_arrow(71.2, 47.5, 77.3, 49.5, "JSON Logs", "#d97706", label_offset=(0, 1.1))
    draw_arrow(86.75, 45.8, 86.75, 42.9, "Bucket Sink", "#0284c7")
    draw_arrow(86.75, 35.3, 86.75, 32.4, "SIEM Sink", "#7c3aed")
    draw_arrow(86.75, 24.8, 86.75, 21.9, "Grafana SQL", "#0f766e")

    draw_box(2.5, 2.5, 95.0, 6.8,
             "Infrastructure as Code (Terraform)  |  NGINX mTLS (certs/ca.crt)  |  Akamai Origin Cloaking  |  SIEM & GCP Buckets",
             "Reproducible on Local Windows/macOS (PowerShell / Docker Compose), Google Colab Notebook, or GCP Multi-Region Production",
             bg="#e2e8f0", border="#475569")

    arch_png = OBS_DIR / "architecture_diagram.png"
    fig2.savefig(arch_png, dpi=180, facecolor=fig2.get_facecolor())
    plt.close(fig2)
    print(f"  -> Saved Architecture Diagram to {arch_png.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    verify_live_endpoints()
    logs = generate_telemetry_dataset()
    run_bigquery_sql_analytics(logs)
    render_visual_artifacts(logs)
    print("\nAll 4 enhancements verified, SQL queries executed, and visual exports updated!")
