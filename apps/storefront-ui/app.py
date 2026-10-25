#!/usr/bin/env python3
"""
Web Application A: Meridian Storefront UI (storefront-ui)
Enhanced with:
1. Blue Header & Very Light Gray Page Background UI Theme
2. Traffic Flip Management: DRAIN_REGION readiness drain flag (/readyz returns HTTP 503 when drained)
   plus live /api/admin/drain toggle endpoint & working Toggle Region Drain button.
3. Akamai CDN Client IP & Trace Preservation: Extracts and forwards True-Client-IP,
   Akamai-Request-BC, and X-Cloud-Trace-Context to Web App B (order-service).
4. Ingress-level NGINX mTLS enforcement; application code does not trust
   forwarded client-certificate headers as authenticated identity.
5. Intra-Region Advanced Routing: Supports RELEASE_TRACK (stable vs canary) and /v1/catalog rewrite.
"""

import os
import sys
import time
import json
import uuid
import socket
import random
import html
import psycopg
import urllib.request
import urllib.error
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from common.admin_auth import authorize_admin
from common.http_server import BoundedThreadingHTTPServer
from common import database, region_state

SERVICE_NAME = os.environ.get("SERVICE_NAME", "storefront-ui")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "meridian-gke-primary")
CLUSTER_REGION = os.environ.get("CLUSTER_REGION", "us-central1")
NAMESPACE = os.environ.get("POD_NAMESPACE", "meridian-prod")
POD_NAME = os.environ.get("POD_NAME", socket.gethostname())
RELEASE_TRACK = os.environ.get("RELEASE_TRACK", "stable")
ORDER_API_URL = os.environ.get("ORDER_API_URL", "http://localhost:8081")
SITE_TITLE = os.environ.get("SITE_TITLE", "Meridian Supply Co.")

DRAIN_REGION_DEFAULT = os.environ.get("DRAIN_REGION", "false").lower() == "true"

CATALOG = [
    {"sku": "MRD-101", "name": "Titanium Field Chronograph", "category": "Navigation", "price": 245.00, "stock": 42},
    {"sku": "MRD-204", "name": "All-Weather Expedition Pack 45L", "category": "Gear", "price": 189.00, "stock": 19},
    {"sku": "MRD-309", "name": "Solar Telemetry Beacon", "category": "Electronics", "price": 320.00, "stock": 11},
    {"sku": "MRD-412", "name": "Thermal Insulated Base Layer", "category": "Apparel", "price": 95.00, "stock": 84},
]


def extract_telemetry_headers(headers: dict):
    norm = {k.lower(): v for k, v in (headers or {}).items()}
    trace_id = norm.get("x-cloud-trace-context", uuid.uuid4().hex[:32]).split("/")[0]
    true_client_ip = norm.get("true-client-ip") or norm.get("x-forwarded-for", "").split(",")[0].strip() or None
    akamai_bc = norm.get("akamai-request-bc")
    release_override = norm.get("x-release-track", RELEASE_TRACK)
    return trace_id, true_client_ip, akamai_bc, None, "NOT_VERIFIED_BY_APPLICATION", None, release_override


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
        "release_track": RELEASE_TRACK,
        "true_client_ip": true_client_ip,
        "akamai_request_bc": akamai_bc,
        "mtls_client_subject": mtls_subject,
        "mtls_verification": "NOT_VERIFIED_BY_APPLICATION",
        "log_transport": "stdout",
        "latency_ms": round(latency_ms, 2),
        "status_code": http_status,
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
    trace_id, true_client_ip, akamai_bc, mtls_subject, mtls_verified, mtls_certificate, track = extract_telemetry_headers(headers)

    if path == "/v1/catalog":
        path = "/api/catalog"

    if path == "/healthz":
        return 200, "application/json", json.dumps({
            "status": "healthy",
            "service": SERVICE_NAME,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "release_track": track,
            "pod": POD_NAME,
        }).encode("utf-8")

    if path == "/readyz":
        if region_state.is_drained(CLUSTER_REGION, DRAIN_REGION_DEFAULT):
            latency_ms = (time.perf_counter() - start) * 1000.0
            emit_structured_log(
                "WARNING",
                f"Region {CLUSTER_REGION} ({CLUSTER_NAME}) is drained; returning 503 readiness",
                method, 503, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject,
                {"drain_region": True}
            )
            return 503, "application/json", json.dumps({
                "ready": False,
                "reason": "REGION_DRAINED_BY_OPERATOR",
                "cluster": CLUSTER_NAME,
                "region": CLUSTER_REGION,
            }).encode("utf-8")

        return 200, "application/json", json.dumps({
            "ready": True,
            "service": SERVICE_NAME,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "release_track": track,
            "pod": POD_NAME,
        }).encode("utf-8")

    if path == "/api/admin/drain":
        if method != "POST":
            return 405, "application/json", json.dumps({"error": "POST required"}).encode("utf-8")
        auth_status, admin_identity = authorize_admin(headers)
        if auth_status != 200:
            latency_ms = (time.perf_counter() - start) * 1000.0
            emit_structured_log(
                "ERROR",
                "Admin drain request rejected by identity authorization",
                method, auth_status, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject,
                {"authorization_status": auth_status}
            )
            return auth_status, "application/json", json.dumps({"error": "Admin authorization failed"}).encode("utf-8")

        try:
            body_json = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, "application/json", json.dumps({"error": "Expected JSON body"}).encode("utf-8")
        if not isinstance(body_json, dict) or not isinstance(body_json.get("drain"), bool):
            return 400, "application/json", json.dumps({"error": "drain must be a boolean"}).encode("utf-8")
        try:
            region_state.set_drained(CLUSTER_REGION, body_json["drain"])
        except psycopg.Error:
            return 503, "application/json", json.dumps({"error": "Regional control store unavailable"}).encode("utf-8")

        latency_ms = (time.perf_counter() - start) * 1000.0
        state_label = "DRAINED (readiness returns 503)" if body_json["drain"] else "ACTIVE (readiness returns 200)"
        emit_structured_log(
            "WARNING" if body_json["drain"] else "INFO",
            f"Regional traffic state updated: {state_label}",
            method, 200, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject,
            {"drain_region": body_json["drain"], "admin_identity": admin_identity}
        )
        return 200, "application/json", json.dumps({
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "drain_region": body_json["drain"],
            "readyz_status_code": 503 if body_json["drain"] else 200,
            "status_summary": state_label,
        }).encode("utf-8")

    if path == "/api/catalog" and method == "GET":
        if os.environ.get("APP_ENV", "local").lower() == "local":
            time.sleep(random.uniform(0.008, 0.035))

        if os.environ.get("APP_ENV", "local").lower() == "local" and query.get("simulate_error", ["false"])[0] == "true":
            latency_ms = (time.perf_counter() - start) * 1000.0
            emit_structured_log(
                "ERROR",
                "Catalog cache lookup timeout while querying regional replica",
                method, 503, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject,
                {"error_code": "CATALOG_REPLICA_TIMEOUT"}
            )
            return 503, "application/json", json.dumps({"error": "Catalog temporarily unavailable", "trace_id": trace_id}).encode("utf-8")

        latency_ms = (time.perf_counter() - start) * 1000.0
        emit_structured_log("INFO", "Served product catalog successfully", method, 200, latency_ms, path,
                            trace_id, true_client_ip, akamai_bc, mtls_subject)
        return 200, "application/json", json.dumps({
            "site": SITE_TITLE,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "release_track": track,
            "pod": POD_NAME,
            "true_client_ip": true_client_ip,
            "akamai_request_bc": akamai_bc,
            "mtls_client_subject": mtls_subject,
            "mtls_verified": mtls_verified,
            "items": CATALOG,
            "trace_id": trace_id,
        }).encode("utf-8")

    if path == "/api/checkout" and method == "POST":
        try:
            payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {"sku": "MRD-101", "quantity": 1}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"sku": "MRD-101", "quantity": 1}

        forwarded_headers = {
            "Content-Type": "application/json",
            "X-Cloud-Trace-Context": trace_id,
            "True-Client-IP": true_client_ip or "",
            "Akamai-Request-BC": akamai_bc or "",
            "X-Release-Track": track,
        }
        incoming_headers = {key.lower(): value for key, value in headers.items()}
        forwarded_headers["Idempotency-Key"] = incoming_headers.get("idempotency-key", uuid.uuid4().hex)
        req = urllib.request.Request(
            f"{ORDER_API_URL}/api/orders",
            data=json.dumps(payload).encode("utf-8"),
            headers=forwarded_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                resp_body = resp.read()
                status_code = resp.getcode()
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            resp_body = json.dumps({"error": f"Upstream order-service error ({exc.code})", "trace_id": trace_id}).encode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            status_code = 502
            resp_body = json.dumps({"error": f"Failed to reach order-service: {str(exc)}", "trace_id": trace_id}).encode("utf-8")

        latency_ms = (time.perf_counter() - start) * 1000.0
        severity = "ERROR" if status_code >= 500 else ("WARNING" if status_code >= 400 else "INFO")
        emit_structured_log(severity, f"Checkout completed via order-service with status {status_code}",
                            method, status_code, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject)
        return status_code, "application/json", resp_body

    if path == "/" and method == "GET":
        drain_region = region_state.is_drained(CLUSTER_REGION, DRAIN_REGION_DEFAULT)
        drain_badge_color = "#dc2626" if drain_region else "#16a34a"
        drain_badge_text = "DRAINED (503)" if drain_region else "READY (200)"
        cards_html = "".join([
            f"""
            <div class="card">
              <div class="sku">{item['sku']} &bull; {item['category']}</div>
              <h3>{item['name']}</h3>
              <div class="price">${item['price']:.2f}</div>
              <div class="stock">In Stock: {item['stock']} units</div>
              <button onclick="placeOrder('{item['sku']}')">Order Now</button>
            </div>
            """
            for item in CATALOG
        ])
        site_title_html = html.escape(SITE_TITLE)
        cluster_name_html = html.escape(CLUSTER_NAME)
        cluster_region_html = html.escape(CLUSTER_REGION)
        release_track_html = html.escape(str(track))
        client_ip_html = html.escape(str(true_client_ip or "Not provided"))
        trace_id_html = html.escape(str(trace_id[:16]))
        page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{site_title_html} | Multi-Cluster Storefront</title>
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
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1.25rem; margin-top: 1.25rem; }}
    .card {{ background: var(--card); border: 1px solid #e2e8f0; border-radius: 10px; padding: 1.35rem; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
    .sku {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }}
    .card h3 {{ margin: 0.5rem 0; color: #0f172a; font-size: 1.1rem; }}
    .price {{ font-size: 1.45rem; font-weight: 700; color: #1d4ed8; margin: 0.5rem 0; }}
    .stock {{ font-size: 0.85rem; color: #475569; margin-bottom: 1.1rem; }}
    button {{ background: var(--accent); color: #ffffff; border: none; padding: 0.65rem 1rem; border-radius: 6px; font-weight: 600; cursor: pointer; width: 100%; transition: background 0.15s; }}
    button:hover {{ background: #1d4ed8; }}
    .btn-drain {{ background: #1e293b; color: #ffffff; width: auto; padding: 0.4rem 0.85rem; font-size: 0.78rem; }}
    .btn-drain:hover {{ background: #334155; }}
    #status-box {{ margin-top: 1.5rem; padding: 1.1rem; border-radius: 8px; background: #ffffff; border: 1px solid #cbd5e1; border-left: 4px solid var(--accent); font-family: monospace; font-size: 0.86rem; color: #0f172a; white-space: pre-wrap; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
  </style>
</head>
<body>
  <header>
    <div>
      <strong style="font-size: 1.3rem;">{site_title_html}</strong>
      <span style="color: #dbeafe; margin-left: 0.75rem;">Global Edge Storefront (Web App A)</span>
    </div>
    <div style="display: flex; gap: 0.45rem; flex-wrap: wrap;">
      <span class="badge">Cluster: {cluster_name_html}</span>
      <span class="badge">Region: {cluster_region_html}</span>
      <span class="badge">Track: {release_track_html}</span>
      <span class="badge" id="drain-badge" style="background: {drain_badge_color}; border-color: transparent;">Readiness: {drain_badge_text}</span>
    </div>
  </header>
  <div class="sec-bar">
    <div>
      <span>Client IP (trusted proxy header): <strong style="color:#1d4ed8;">{client_ip_html}</strong></span> &nbsp;|&nbsp;
      <span>mTLS App Identity: <strong style="color:#15803d;">Not asserted</strong></span> &nbsp;|&nbsp;
      <span>Trace ID: <strong style="color:#b45309;">{trace_id_html}</strong></span>
    </div>
    <div style="display:flex; align-items:center; gap:0.5rem;">
      <button type="button" class="btn-drain" onclick="toggleRegionDrain()">Toggle Regional Readiness</button>
    </div>
  </div>
  <main>
    <h2 style="margin-bottom: 0.35rem;">Field Equipment Catalog</h2>
    <p style="color: var(--muted); margin-top: 0;">Public storefront via Akamai and Cloud Armor; partner API uses a separate NGINX mTLS ingress. Release track: {release_track_html}.</p>
    <div class="grid">{cards_html}</div>
    <div id="status-box">Ready. Click "Order Now" to invoke Web App B (Order Service), or toggle regional readiness to make this instance return HTTP 503 from /readyz. External traffic routing must be configured separately.</div>
  </main>
  <script>
    async function placeOrder(sku) {{
      var box = document.getElementById('status-box');
      box.textContent = 'Submitting order for ' + sku + ' to Order Service (Web App B)...';
      try {{
        var res = await fetch('/api/checkout', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() }},
          body: JSON.stringify({{ sku: sku, quantity: 1 }})
        }});
        var data = await res.json();
        box.textContent = 'Order Response [HTTP ' + res.status + ']:\\n' + JSON.stringify(data, null, 2);
      }} catch (err) {{
        box.textContent = 'Error: ' + err.message;
      }}
    }}

    async function toggleRegionDrain() {{
      var box = document.getElementById('status-box');
      var token = prompt('Paste a local demo token or a Google OIDC ID token authorized for regional operations:');
      if (!token) return;
      var badge = document.getElementById('drain-badge');
      var shouldDrain = !badge.textContent.includes('DRAINED');
      box.textContent = 'Updating regional readiness drain state...';
      try {{
        var res = await fetch('/api/admin/drain', {{
          method: 'POST',
          headers: {{ 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ drain: shouldDrain }})
        }});
        var data = await res.json();
        if (res.ok) {{
          badge.style.background = data.drain_region ? '#dc2626' : '#16a34a';
          badge.textContent = 'Readiness: ' + (data.drain_region ? 'DRAINED (503)' : 'READY (200)');
        }}
        box.textContent = 'Regional Readiness Update:\\n' + JSON.stringify(data, null, 2);
      }} catch (err) {{
        box.textContent = 'Drain toggle error: ' + err.message;
      }}
    }}
  </script>
</body>
</html>"""
        latency_ms = (time.perf_counter() - start) * 1000.0
        emit_structured_log("INFO", "Rendered Storefront UI homepage", method, 200, latency_ms, path,
                            trace_id, true_client_ip, akamai_bc, mtls_subject)
        return 200, "text/html; charset=utf-8", page_html.encode("utf-8")

    return 404, "application/json", json.dumps({"error": "Not Found", "path": path}).encode("utf-8")


class StorefrontHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def do_GET(self):
        status, content_type, body = process_request("GET", self.path, dict(self.headers))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if length < 0 or length > 65536:
            self.send_error(413, "Request body too large")
            return
        body_bytes = self.rfile.read(length) if length > 0 else b""
        if len(body_bytes) != length:
            self.send_error(400, "Incomplete request body")
            return
        status, content_type, body = process_request("POST", self.path, dict(self.headers), body_bytes)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    database.wait_for_database()
    region_state.initialize()
    port = int(os.environ.get("PORT", 8080))
    server = BoundedThreadingHTTPServer(("0.0.0.0", port), StorefrontHandler)
    print(json.dumps({"severity": "INFO", "message": f"Starting {SERVICE_NAME} on port {port}", "cluster_name": CLUSTER_NAME}), flush=True)
    server.serve_forever()
