#!/usr/bin/env python3
"""
Web Application B: Meridian Order & Fulfillment API + Console UI (order-service)
Enhanced with:
1. Interactive HTML Console at GET / (for localhost:8081 & localhost:8083 in Docker Desktop)
   with Blue Header and Very Light Gray Page Background.
2. Traffic Flip Management: DRAIN_REGION readiness drain flag (/readyz returns HTTP 503 when drained)
   plus /api/admin/drain toggle endpoint.
3. Akamai True-Client-IP & Akamai-Request-BC + X-Cloud-Trace-Context preservation in logs & order records.
4. NGINX mTLS Client Certificate Subject DN logging.
"""

import os
import time
import json
import uuid
import socket
import random
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

SERVICE_NAME = os.environ.get("SERVICE_NAME", "order-service")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "meridian-gke-primary")
CLUSTER_REGION = os.environ.get("CLUSTER_REGION", "us-central1")
NAMESPACE = os.environ.get("POD_NAMESPACE", "meridian-prod")
POD_NAME = os.environ.get("POD_NAME", socket.gethostname())
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "projects/meridian-cloud/topics/order-events")
SITE_TITLE = os.environ.get("SITE_TITLE", "Meridian Supply Co.")

RUNTIME_STATE = {
    "drain_region": os.environ.get("DRAIN_REGION", "false").lower() == "true"
}

LOCAL_ORDER_CACHE = []


def extract_telemetry_headers(headers: dict):
    norm = {k.lower(): v for k, v in (headers or {}).items()}
    trace_id = norm.get("x-cloud-trace-context", uuid.uuid4().hex[:32]).split("/")[0]
    true_client_ip = (
        norm.get("true-client-ip")
        or norm.get("x-forwarded-for", "").split(",")[0].strip()
        or "198.51.100.42"
    )
    akamai_bc = norm.get("akamai-request-bc", f"[a=23.45.12.88,b={trace_id[:8]},c=g,n=US_OH_DELAWARE]")
    mtls_subject = (
        norm.get("ssl-client-subject-dn")
        or norm.get("x-client-cert-subject-dn")
        or "CN=b2b-partner-client,O=Meridian-Partner"
    )
    release_track = norm.get("x-release-track", "stable")
    return trace_id, true_client_ip, akamai_bc, mtls_subject, release_track


def emit_structured_log(severity: str, message: str, method: str, http_status: int, latency_ms: float, path: str,
                        trace_id: str, true_client_ip: str, akamai_bc: str, mtls_subject: str, extra: dict = None):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "severity": severity,
        "message": message,
        "trace_id": trace_id,
        "trace": trace_id,
        "log_gapis_trace": trace_id,
        "httpRequest": {
            "requestMethod": method,
            "requestUrl": path,
            "status": http_status,
            "remoteIp": true_client_ip,
            "latency": f"{latency_ms / 1000.0:.4f}s",
        },
        "service": SERVICE_NAME,
        "cluster_name": CLUSTER_NAME,
        "region": CLUSTER_REGION,
        "namespace": NAMESPACE,
        "pod_name": POD_NAME,
        "true_client_ip": true_client_ip,
        "akamai_request_bc": akamai_bc,
        "mtls_client_subject": mtls_subject,
        "log_destination": "gcp_log_bucket_and_siem",
        "latency_ms": round(latency_ms, 2),
        "status_code": http_status,
        "cpu_utilization_pct": round(random.uniform(28.0, 79.0), 2),
        "memory_used_mb": round(random.uniform(165.0, 380.0), 1),
    }
    if extra:
        record.update(extra)
    print(json.dumps(record), flush=True)
    return record


def process_request(method: str, raw_path: str, headers: dict = None, body_bytes: bytes = b""):
    start = time.perf_counter()
    headers = headers or {}
    parsed = urlparse(raw_path)
    path = parsed.path
    query = parse_qs(parsed.query)
    trace_id, true_client_ip, akamai_bc, mtls_subject, release_track = extract_telemetry_headers(headers)

    if path == "/healthz":
        return 200, "application/json", json.dumps({
            "status": "healthy",
            "service": SERVICE_NAME,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "pod": POD_NAME,
        }).encode("utf-8")

    if path == "/readyz":
        if RUNTIME_STATE["drain_region"] or os.environ.get("DRAIN_REGION", "false").lower() == "true":
            return 503, "application/json", json.dumps({
                "ready": False,
                "reason": "REGION_DRAINED_BY_OPERATOR",
                "cluster": CLUSTER_NAME,
                "region": CLUSTER_REGION,
                "failover_target": "us-east1",
            }).encode("utf-8")
        return 200, "application/json", json.dumps({
            "ready": True,
            "service": SERVICE_NAME,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "pod": POD_NAME,
        }).encode("utf-8")

    if path == "/api/admin/drain" and method in ("POST", "GET"):
        enable_str = query.get("enable", [None])[0]
        if enable_str is not None:
            RUNTIME_STATE["drain_region"] = (enable_str.lower() == "true")
        elif body_bytes:
            try:
                body_json = json.loads(body_bytes.decode("utf-8"))
                RUNTIME_STATE["drain_region"] = bool(body_json.get("drain", True))
            except Exception:
                RUNTIME_STATE["drain_region"] = not RUNTIME_STATE["drain_region"]
        else:
            RUNTIME_STATE["drain_region"] = not RUNTIME_STATE["drain_region"]

        latency_ms = (time.perf_counter() - start) * 1000.0
        state_label = "DRAINED (Failover Active)" if RUNTIME_STATE["drain_region"] else "ACTIVE (Receiving Traffic)"
        emit_structured_log(
            "WARNING" if RUNTIME_STATE["drain_region"] else "INFO",
            f"Order-service regional traffic state updated: {state_label}",
            method, 200, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject,
            {"drain_region": RUNTIME_STATE["drain_region"]}
        )
        return 200, "application/json", json.dumps({
            "service": SERVICE_NAME,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "drain_region": RUNTIME_STATE["drain_region"],
            "readyz_status_code": 503 if RUNTIME_STATE["drain_region"] else 200,
            "status_summary": state_label,
        }).encode("utf-8")

    if path == "/api/orders" and method == "GET":
        latency_ms = (time.perf_counter() - start) * 1000.0
        emit_structured_log("INFO", "Fetched recent orders list", method, 200, latency_ms, path,
                            trace_id, true_client_ip, akamai_bc, mtls_subject)
        return 200, "application/json", json.dumps({
            "service": SERVICE_NAME,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "pod": POD_NAME,
            "recent_orders": LOCAL_ORDER_CACHE[-20:],
        }).encode("utf-8")

    if path == "/api/orders" and method == "POST":
        try:
            payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            payload = {}

        sku = payload.get("sku", "MRD-101")
        quantity = int(payload.get("quantity", 1))
        sim_delay = float(payload.get("delay_ms", random.uniform(12.0, 48.0)))
        time.sleep(min(sim_delay / 1000.0, 0.20))

        if payload.get("simulate_error") or query.get("simulate_error", ["false"])[0] == "true":
            latency_ms = (time.perf_counter() - start) * 1000.0
            emit_structured_log(
                "ERROR",
                f"Deadlock detected acquiring inventory lock for SKU {sku} in regional store",
                method, 500, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject,
                {"error_code": "INVENTORY_LOCK_TIMEOUT", "sku": sku}
            )
            return 500, "application/json", json.dumps({"error": "Inventory lock timeout", "sku": sku, "trace_id": trace_id}).encode("utf-8")

        order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
        order_record = {
            "order_id": order_id,
            "sku": sku,
            "quantity": quantity,
            "status": "CONFIRMED",
            "processed_by_cluster": CLUSTER_NAME,
            "processed_by_region": CLUSTER_REGION,
            "processed_by_pod": POD_NAME,
            "release_track": release_track,
            "true_client_ip": true_client_ip,
            "akamai_request_bc": akamai_bc,
            "mtls_client_subject": mtls_subject,
            "pubsub_topic": PUBSUB_TOPIC,
            "trace_id": trace_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        LOCAL_ORDER_CACHE.append(order_record)

        latency_ms = (time.perf_counter() - start) * 1000.0
        emit_structured_log(
            "INFO",
            f"Order {order_id} confirmed and published to event stream",
            method, 201, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject,
            {"order_id": order_id, "sku": sku}
        )
        return 201, "application/json", json.dumps(order_record).encode("utf-8")

    # Root HTML Dashboard UI for Web App B (localhost:8081 and localhost:8083)
    if path == "/" and method == "GET":
        drain_badge_color = "#dc2626" if RUNTIME_STATE["drain_region"] else "#16a34a"
        drain_badge_text = "DRAINED (503)" if RUNTIME_STATE["drain_region"] else "READY (200)"
        recent_json = json.dumps(LOCAL_ORDER_CACHE[-10:][::-1], indent=2)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{SITE_TITLE} | Order & Fulfillment API Console (Web App B)</title>
  <style>
    :root {{
      --bg: #f3f4f6;
      --card: #ffffff;
      --header-blue: #1d4ed8;
      --header-blue-dark: #1e3a8a;
      --accent: #2563eb;
      --text: #0f172a;
      --muted: #64748b;
    }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 0; }}
    header {{ background: linear-gradient(90deg, var(--header-blue-dark) 0%, var(--header-blue) 100%); color: #ffffff; padding: 1.15rem 2rem; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 0.75rem; box-shadow: 0 2px 6px rgba(0,0,0,0.08); }}
    .badge {{ background: rgba(255, 255, 255, 0.18); color: #ffffff; border: 1px solid rgba(255, 255, 255, 0.3); padding: 0.32rem 0.7rem; border-radius: 6px; font-size: 0.78rem; font-family: monospace; }}
    .sec-bar {{ background: #e2e8f0; border-bottom: 1px solid #cbd5e1; padding: 0.65rem 2rem; font-family: monospace; font-size: 0.8rem; color: #334155; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 0.5rem; }}
    main {{ max-width: 1120px; margin: 1.75rem auto; padding: 0 1.5rem; }}
    .panel {{ background: var(--card); border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.35rem; margin-bottom: 1.25rem; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
    .btn-row {{ display: flex; gap: 0.75rem; flex-wrap: wrap; margin-top: 0.75rem; }}
    button {{ background: var(--accent); color: #ffffff; border: none; padding: 0.6rem 1.1rem; border-radius: 6px; font-weight: 600; cursor: pointer; transition: background 0.15s; }}
    button:hover {{ background: #1d4ed8; }}
    .btn-secondary {{ background: #1e293b; }}
    .btn-secondary:hover {{ background: #334155; }}
    pre {{ background: #f8fafc; border: 1px solid #cbd5e1; border-left: 4px solid var(--accent); padding: 1rem; border-radius: 8px; font-family: monospace; font-size: 0.85rem; overflow-x: auto; color: #0f172a; }}
  </style>
</head>
<body>
  <header>
    <div>
      <strong style="font-size: 1.3rem;">{SITE_TITLE}</strong>
      <span style="color: #dbeafe; margin-left: 0.75rem;">Order &amp; Fulfillment Microservice (Web App B)</span>
    </div>
    <div style="display: flex; gap: 0.45rem; flex-wrap: wrap;">
      <span class="badge">Service: {SERVICE_NAME}</span>
      <span class="badge">Cluster: {CLUSTER_NAME}</span>
      <span class="badge">Region: {CLUSTER_REGION}</span>
      <span class="badge" id="drain-badge" style="background: {drain_badge_color}; border-color: transparent;">Readiness: {drain_badge_text}</span>
    </div>
  </header>
  <div class="sec-bar">
    <div>
      <span>Akamai True-Client-IP: <strong style="color:#1d4ed8;">{true_client_ip}</strong></span> &nbsp;|&nbsp;
      <span>mTLS Client DN: <strong style="color:#15803d;">{mtls_subject}</strong></span> &nbsp;|&nbsp;
      <span>Pub/Sub Topic: <strong style="color:#b45309;">{PUBSUB_TOPIC}</strong></span>
    </div>
    <div>
      <button type="button" class="btn-secondary" style="padding: 0.4rem 0.85rem; font-size: 0.78rem;" onclick="toggleRegionDrain()">Toggle Region Drain (/readyz Failover)</button>
    </div>
  </div>
  <main>
    <div class="panel">
      <h2 style="margin-top: 0; margin-bottom: 0.4rem;">Order Fulfillment API Console</h2>
      <p style="color: var(--muted); margin-top: 0;">Backend REST microservice receiving checkout requests from Storefront UI and publishing events to Cloud Pub/Sub &amp; GCP Log Bucket / SIEM.</p>
      <div class="btn-row">
        <button type="button" onclick="createTestOrder('MRD-101')">Create Test Order (MRD-101)</button>
        <button type="button" onclick="refreshOrders()">Refresh Orders List (GET /api/orders)</button>
        <button type="button" class="btn-secondary" onclick="checkHealth('/readyz')">Check Readiness (/readyz)</button>
        <button type="button" class="btn-secondary" onclick="checkHealth('/healthz')">Check Liveness (/healthz)</button>
      </div>
    </div>
    <div class="panel">
      <h3 style="margin-top: 0;">Live Order Ledger &amp; API Output (<span id="order-count">{len(LOCAL_ORDER_CACHE)}</span> total orders)</h3>
      <pre id="output-box">{recent_json}</pre>
    </div>
  </main>
  <script>
    async function createTestOrder(sku) {{
      var box = document.getElementById('output-box');
      box.textContent = 'Creating order for ' + sku + '...';
      try {{
        var res = await fetch('/api/orders', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ sku: sku, quantity: 1 }})
        }});
        var data = await res.json();
        await refreshOrders();
      }} catch (err) {{
        box.textContent = 'Error: ' + err.message;
      }}
    }}

    async function refreshOrders() {{
      var box = document.getElementById('output-box');
      var res = await fetch('/api/orders');
      var data = await res.json();
      document.getElementById('order-count').textContent = data.recent_orders.length;
      box.textContent = JSON.stringify(data, null, 2);
    }}

    async function checkHealth(endpoint) {{
      var box = document.getElementById('output-box');
      var res = await fetch(endpoint);
      var data = await res.json();
      box.textContent = 'GET ' + endpoint + ' [HTTP ' + res.status + ']:\\n' + JSON.stringify(data, null, 2);
    }}

    async function toggleRegionDrain() {{
      var box = document.getElementById('output-box');
      var res = await fetch('/api/admin/drain', {{ method: 'POST' }});
      var data = await res.json();
      var badge = document.getElementById('drain-badge');
      badge.style.background = data.drain_region ? '#dc2626' : '#16a34a';
      badge.textContent = 'Readiness: ' + (data.drain_region ? 'DRAINED (503)' : 'READY (200)');
      box.textContent = 'Traffic Flip Update (/readyz probe):\\n' + JSON.stringify(data, null, 2);
    }}
  </script>
</body>
</html>"""
        latency_ms = (time.perf_counter() - start) * 1000.0
        emit_structured_log("INFO", "Rendered Order Service console UI", method, 200, latency_ms, path,
                            trace_id, true_client_ip, akamai_bc, mtls_subject)
        return 200, "text/html; charset=utf-8", html.encode("utf-8")

    return 404, "application/json", json.dumps({"error": "Not Found", "path": path}).encode("utf-8")


class OrderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, content_type, body = process_request("GET", self.path, dict(self.headers))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length) if length > 0 else b""
        status, content_type, body = process_request("POST", self.path, dict(self.headers), body_bytes)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    server = ThreadingHTTPServer(("0.0.0.0", port), OrderHandler)
    print(json.dumps({"severity": "INFO", "message": f"Starting {SERVICE_NAME} on port {port}", "cluster_name": CLUSTER_NAME}), flush=True)
    server.serve_forever()
