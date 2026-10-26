#!/usr/bin/env python3
"""
Web Application B: Meridian Order & Fulfillment API + Console UI (order-service)
Enhanced with:
1. Interactive HTML Console at GET / (for localhost:8081 & localhost:8083 in Docker Desktop)
   with Blue Header and Very Light Gray Page Background.
2. Traffic Flip Management: DRAIN_REGION readiness drain flag (/readyz returns HTTP 503 when drained)
   plus /api/admin/drain toggle endpoint.
3. Akamai True-Client-IP & Akamai-Request-BC + X-Cloud-Trace-Context preservation in logs & order records.
4. Ingress-level mTLS enforcement; forwarded client-certificate headers are
   not treated as authenticated application identity.
"""

import os
import sys
import time
import json
import uuid
import socket
import random
import hashlib
import html
import threading
import urllib.error
import psycopg
import psycopg
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from common.admin_auth import authorize_admin
from common.http_server import BoundedThreadingHTTPServer
from common import database, region_state
import order_store

SERVICE_NAME = os.environ.get("SERVICE_NAME", "order-service")
VALID_SKUS = {"MRD-101", "MRD-204", "MRD-309", "MRD-412"}
CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "meridian-gke-primary")
CLUSTER_REGION = os.environ.get("CLUSTER_REGION", "us-central1")
NAMESPACE = os.environ.get("POD_NAMESPACE", "meridian-prod")
POD_NAME = os.environ.get("POD_NAME", socket.gethostname())
SITE_TITLE = os.environ.get("SITE_TITLE", "Meridian Supply Co.")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "")

DRAIN_REGION_DEFAULT = os.environ.get("DRAIN_REGION", "false").lower() == "true"

def _trusted_upstream_cidrs():
    raw = os.environ.get("TRUSTED_UPSTREAM_CIDRS", "")
    return tuple(cidr.strip() for cidr in raw.split(",") if cidr.strip())


def _peer_is_trusted(peer_ip, trusted):
    """Return True if the TCP peer is inside a trusted upstream CIDR, or if
    no trusted set is configured (local-dev ergonomics)."""
    if not trusted:
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(peer_ip)
    except (TypeError, ValueError):
        return False
    for cidr in trusted:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def extract_telemetry_headers(headers: dict, peer_ip: str = None):
    norm = {k.lower(): v for k, v in (headers or {}).items()}
    trace_id = norm.get("x-cloud-trace-context", uuid.uuid4().hex[:32]).split("/")[0]
    trusted = _trusted_upstream_cidrs()
    if peer_ip and not _peer_is_trusted(peer_ip, trusted):
        true_client_ip = None
    else:
        true_client_ip = norm.get("true-client-ip") or norm.get("x-forwarded-for", "").split(",")[0].strip() or None
    akamai_bc = norm.get("akamai-request-bc")
    release_track = norm.get("x-release-track", "stable")
    return trace_id, true_client_ip, akamai_bc, None, "NOT_VERIFIED_BY_APPLICATION", release_track


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
        "mtls_verification": "NOT_VERIFIED_BY_APPLICATION",
        "log_transport": "stdout",
        "latency_ms": round(latency_ms, 2),
        "status_code": http_status,
    }
    if extra:
        record.update(extra)
    print(json.dumps(record), flush=True)
    return record


def process_request(method: str, raw_path: str, headers: dict = None, body_bytes: bytes = b"", peer_ip: str = None):
    start = time.perf_counter()
    headers = headers or {}
    parsed = urlparse(raw_path)
    path = parsed.path
    query = parse_qs(parsed.query)
    trace_id, true_client_ip, akamai_bc, mtls_subject, mtls_verified, release_track = extract_telemetry_headers(headers, peer_ip)

    if path == "/healthz":
        return 200, "application/json", json.dumps({
            "status": "healthy",
            "service": SERVICE_NAME,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "pod": POD_NAME,
        }).encode("utf-8")

    if path == "/readyz":
        if region_state.is_drained(CLUSTER_REGION, DRAIN_REGION_DEFAULT):
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
        state_label = "REGION DRAINED" if body_json["drain"] else "REGION ACTIVE"
        emit_structured_log(
            "WARNING" if body_json["drain"] else "INFO",
            f"Order-service regional traffic state updated: {state_label}",
            method, 200, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject,
            {"drain_region": body_json["drain"], "admin_identity": admin_identity}
        )
        return 200, "application/json", json.dumps({
            "service": SERVICE_NAME,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "drain_region": body_json["drain"],
            "readyz_status_code": 503 if body_json["drain"] else 200,
            "status_summary": state_label,
        }).encode("utf-8")

    if path == "/api/orders" and method == "GET":
        auth_status, _ = authorize_admin(headers)
        if auth_status != 200:
            return auth_status, "application/json", json.dumps({"error": "Order listing requires admin authorization"}).encode("utf-8")
        try:
            recent_orders = order_store.list_recent()
        except psycopg.Error:
            return 503, "application/json", json.dumps({"error": "Order store unavailable"}).encode("utf-8")
        latency_ms = (time.perf_counter() - start) * 1000.0
        emit_structured_log("INFO", "Fetched recent orders list", method, 200, latency_ms, path,
                            trace_id, true_client_ip, akamai_bc, mtls_subject)
        return 200, "application/json", json.dumps({
            "service": SERVICE_NAME,
            "cluster": CLUSTER_NAME,
            "region": CLUSTER_REGION,
            "pod": POD_NAME,
            "recent_orders": recent_orders,
        }).encode("utf-8")

    if path == "/api/orders" and method == "POST":
        try:
            payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, "application/json", json.dumps({"error": "Expected valid JSON"}).encode("utf-8")
        if not isinstance(payload, dict):
            return 400, "application/json", json.dumps({"error": "JSON object required"}).encode("utf-8")
        sku = payload.get("sku", "MRD-101")
        quantity = payload.get("quantity", 1)
        if not isinstance(sku, str) or sku not in VALID_SKUS:
            return 400, "application/json", json.dumps({"error": "sku is not in the supported catalog"}).encode("utf-8")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
            return 400, "application/json", json.dumps({"error": "quantity must be an integer from 1 to 100"}).encode("utf-8")
        if os.environ.get("APP_ENV", "local").lower() == "local":
            time.sleep(random.uniform(0.012, 0.048))
        if os.environ.get("APP_ENV", "local").lower() == "local" and (
            payload.get("simulate_error") or query.get("simulate_error", ["false"])[0] == "true"
        ):
            latency_ms = (time.perf_counter() - start) * 1000.0
            emit_structured_log(
                "ERROR",
                f"Deadlock detected acquiring inventory lock for SKU {sku} in regional store",
                method, 500, latency_ms, path, trace_id, true_client_ip, akamai_bc, mtls_subject,
                {"error_code": "INVENTORY_LOCK_TIMEOUT", "sku": sku}
            )
            return 500, "application/json", json.dumps({"error": "Inventory lock timeout", "sku": sku, "trace_id": trace_id}).encode("utf-8")

        idempotency_key = next(
            (value for key, value in (headers or {}).items() if key.lower() == "idempotency-key"),
            uuid.uuid4().hex,
        )
        if not idempotency_key or len(idempotency_key) > 128:
            return 400, "application/json", json.dumps({"error": "Idempotency-Key must be 1 to 128 characters"}).encode("utf-8")
        request_hash = hashlib.sha256(json.dumps(
            {"sku": sku, "quantity": quantity}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        order_id = f"ORD-{uuid.uuid4().hex[:12].upper()}"
        event_id = str(uuid.uuid4())
        order_record = {
            "order_id": order_id,
            "sku": sku,
            "quantity": quantity,
            "true_client_ip": true_client_ip,
            "status": "CONFIRMED",
            "processed_by_cluster": CLUSTER_NAME,
            "processed_by_region": CLUSTER_REGION,
            "processed_by_pod": POD_NAME,
            "release_track": release_track,
            "trace_id": trace_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            outcome, stored_order = order_store.create_order(
                order_record, idempotency_key, request_hash, event_id
            )
        except psycopg.Error as exc:
            emit_structured_log(
                "ERROR", "Order persistence failed", method, 503,
                (time.perf_counter() - start) * 1000.0, path,
                trace_id, true_client_ip, akamai_bc, mtls_subject,
                {"database_error": type(exc).__name__}
            )
            return 503, "application/json", json.dumps({"error": "Order persistence unavailable", "trace_id": trace_id}).encode("utf-8")
        if outcome == "conflict":
            return 409, "application/json", json.dumps({"error": "Idempotency-Key was already used for a different order"}).encode("utf-8")
        order_record = order_store.serialize_order(stored_order)

        latency_ms = (time.perf_counter() - start) * 1000.0
        emit_structured_log(
            "INFO",
            f"Order {order_record['order_id']} persisted with transactional outbox",
            method, 201 if outcome == "created" else 200, latency_ms, path,
            trace_id, true_client_ip, akamai_bc, mtls_subject,
            {"order_id": order_record["order_id"], "sku": sku, "outbox_event_id": event_id}
        )
        return (201 if outcome == "created" else 200), "application/json", json.dumps(order_record).encode("utf-8")

    # Root HTML Dashboard UI for Web App B (localhost:8081 and localhost:8083)
    if path == "/" and method == "GET":
        drain_region = region_state.is_drained(CLUSTER_REGION, DRAIN_REGION_DEFAULT)
        drain_badge_color = "#dc2626" if drain_region else "#16a34a"
        drain_badge_text = "DRAINED (503)" if drain_region else "READY (200)"
        recent_json = "Authenticate and select Refresh Orders List to view recent orders."
        site_title_html = html.escape(SITE_TITLE)
        service_name_html = html.escape(SERVICE_NAME)
        cluster_name_html = html.escape(CLUSTER_NAME)
        cluster_region_html = html.escape(CLUSTER_REGION)
        client_ip_html = html.escape(str(true_client_ip or "Not provided"))
        page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{site_title_html} | Order &amp; Fulfillment API Console (Web App B)</title>
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
      <strong style="font-size: 1.3rem;">{site_title_html}</strong>
      <span style="color: #dbeafe; margin-left: 0.75rem;">Order &amp; Fulfillment Microservice (Web App B)</span>
    </div>
    <div style="display: flex; gap: 0.45rem; flex-wrap: wrap;">
      <span class="badge">Service: {service_name_html}</span>
      <span class="badge">Cluster: {cluster_name_html}</span>
      <span class="badge">Region: {cluster_region_html}</span>
      <span class="badge" id="drain-badge" style="background: {drain_badge_color}; border-color: transparent;">Readiness: {drain_badge_text}</span>
    </div>
  </header>
  <div class="sec-bar">
    <div>
      <span>Client IP (trusted proxy header): <strong style="color:#1d4ed8;">{client_ip_html}</strong></span> &nbsp;|&nbsp;
      <span>mTLS App Identity: <strong style="color:#15803d;">Not asserted</strong></span> &nbsp;|&nbsp;
      <span>Orders are stored in PostgreSQL; event delivery is recorded in a transactional outbox.</span>
    </div>
    <div>
      <button type="button" class="btn-secondary" style="padding: 0.4rem 0.85rem; font-size: 0.78rem;" onclick="toggleRegionDrain()">Toggle Regional Readiness</button>
    </div>
  </div>
  <main>
    <div class="panel">
      <h2 style="margin-top: 0; margin-bottom: 0.4rem;">Order Fulfillment API Console</h2>
      <p style="color: var(--muted); margin-top: 0;">Backend REST microservice receiving checkout requests from Storefront UI and durably recording orders and outbox events.</p>
      <div class="btn-row">
        <button type="button" onclick="createTestOrder('MRD-101')">Create Test Order (MRD-101)</button>
        <button type="button" onclick="refreshOrders()">Refresh Orders List (GET /api/orders)</button>
        <button type="button" class="btn-secondary" onclick="checkHealth('/readyz')">Check Readiness (/readyz)</button>
        <button type="button" class="btn-secondary" onclick="checkHealth('/healthz')">Check Liveness (/healthz)</button>
      </div>
    </div>
    <div class="panel">
      <h3 style="margin-top: 0;">Order Ledger &amp; API Output</h3>
      <pre id="output-box">{recent_json}</pre>
    </div>
  </main>
  <script>
    async function createTestOrder(sku) {{
      var box = document.getElementById('output-box');
      box.textContent = 'Creating order for ' + sku + '...';
      try {{
        var idempotencyKey = crypto.randomUUID();
        var res = await fetch('/api/orders', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey }},
          body: JSON.stringify({{ sku: sku, quantity: 1 }})
        }});
        var data = await res.json();
        box.textContent = 'Create order [HTTP ' + res.status + ']:\\n' + JSON.stringify(data, null, 2);
      }} catch (err) {{
        box.textContent = 'Error: ' + err.message;
      }}
    }}

    async function refreshOrders() {{
      var box = document.getElementById('output-box');
      var token = prompt('Paste a local demo token or a Google OIDC ID token authorized to view orders:');
      if (!token) return;
      var res = await fetch('/api/orders', {{ headers: {{ 'Authorization': 'Bearer ' + token }} }});
      var data = await res.json();
      if (!res.ok) {{
        box.textContent = 'Order lookup [HTTP ' + res.status + ']:\\n' + JSON.stringify(data, null, 2);
        return;
      }}
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
      var token = prompt('Paste a local demo token or a Google OIDC ID token authorized for regional operations:');
      if (!token) return;
      var badge = document.getElementById('drain-badge');
      var shouldDrain = !badge.textContent.includes('DRAINED');
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
    }}
  </script>
</body>
</html>"""
        latency_ms = (time.perf_counter() - start) * 1000.0
        emit_structured_log("INFO", "Rendered Order Service console UI", method, 200, latency_ms, path,
                            trace_id, true_client_ip, akamai_bc, mtls_subject)
        return 200, "text/html; charset=utf-8", page_html.encode("utf-8")

    return 404, "application/json", json.dumps({"error": "Not Found", "path": path}).encode("utf-8")


def publish_outbox():
    from google.api_core.exceptions import GoogleAPICallError
    from google.cloud import pubsub_v1

    publisher = pubsub_v1.PublisherClient()
    while True:
        try:
            event = order_store.claim_outbox_event()
        except psycopg.Error as exc:
            print(json.dumps({
                "severity": "ERROR",
                "message": "Could not claim an order outbox event",
                "database_error": type(exc).__name__,
            }), flush=True)
            time.sleep(5)
            continue
        if event is None:
            time.sleep(2)
            continue

        try:
            publisher.publish(
                PUBSUB_TOPIC,
                json.dumps(event["payload"]).encode("utf-8"),
                event_id=event["event_id"],
            ).result(timeout=15)
            order_store.mark_outbox_published(event["event_id"])
        except (GoogleAPICallError, TimeoutError, psycopg.Error, ValueError) as exc:
            print(json.dumps({
                "severity": "ERROR",
                "message": "Order outbox publish failed; event will be retried",
                "event_id": event["event_id"],
                "error_type": type(exc).__name__,
            }), flush=True)
            try:
                order_store.release_outbox_event(event["event_id"])
            except psycopg.Error as release_error:
                print(json.dumps({
                    "severity": "ERROR",
                    "message": "Could not release the failed order outbox claim",
                    "event_id": event["event_id"],
                    "database_error": type(release_error).__name__,
                }), flush=True)
            time.sleep(5)


class OrderHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _peer_ip(self):
        try:
            return self.client_address[0]
        except (IndexError, AttributeError):
            return None

    def do_GET(self):
        status, content_type, body = process_request("GET", self.path, dict(self.headers), peer_ip=self._peer_ip())
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
        status, content_type, body = process_request("POST", self.path, dict(self.headers), body_bytes, peer_ip=self._peer_ip())
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    database.wait_for_database()
    order_store.initialize()
    region_state.initialize()
    if PUBSUB_TOPIC:
        publisher_thread = threading.Thread(target=publish_outbox, daemon=True)
        publisher_thread.start()
    else:
        print(json.dumps({
            "severity": "WARNING",
            "message": "PUBSUB_TOPIC is unset; order events will remain in the transactional outbox",
        }), flush=True)
    port = int(os.environ.get("PORT", 8081))
    server = BoundedThreadingHTTPServer(("0.0.0.0", port), OrderHandler)
    print(json.dumps({"severity": "INFO", "message": f"Starting {SERVICE_NAME} on port {port}", "cluster_name": CLUSTER_NAME}), flush=True)
    server.serve_forever()
