# Edge, Traffic, and Routing Boundaries

**Scope:** who owns which part of the public request path, what this repo configures, and what remains an operator responsibility.

## Three independent layers

Public requests pass through three independent control planes. Each is a distinct trust boundary:

```
┌────────────────────────────┐
│  1. Akamai edge            │  WAF + Site Shield + True-Client-IP injection + x-akamai-origin-secret header
└────────────────────────────┘
             │
             ▼
┌────────────────────────────┐
│  2. GKE Ingress + GCLB     │  Cloud Armor policy (SQLi/XSS, Site Shield allowlist, origin-secret check)
│     (optionally MCI)       │
└────────────────────────────┘
             │
             ▼
┌────────────────────────────┐
│  3. In-cluster NGINX mTLS  │  Separate ingress for partner traffic; client-cert gate (not identity)
└────────────────────────────┘
```

### What this repository configures

| Layer | What is in the repo | Operator responsibility |
|---|---|---|
| 1. Akamai | **Nothing executable.** The Cloud Armor expression names Akamai Site Shield CIDRs (operator-supplied Terraform var) and an origin-secret. | Create Akamai Property; configure Origin Hostname → GCLB IP; set the `X-Akamai-Origin-Secret` request header; attach Kona WAF rules; verify Site Shield CIDRs quarterly. |
| 2. GCLB + Cloud Armor | `terraform/observability_security.tf` defines `google_compute_security_policy.akamai_origin_cloaking_policy` with the three rules (SQLi, XSS, origin-secret allow). `k8s/07-akamai-backend-config.yaml` + `k8s/02-storefront-ui.yaml` BackendConfig annotation attach it to the storefront NEG. | `terraform apply` with `akamai_site_shield_cidrs` and `akamai_origin_secret` variables; verify the policy is attached to the intended backend (Cloud Console → Load Balancer → Backend). |
| 2b. MCI | `k8s/04-ingress-and-mci.yaml` ships a *single-cluster* Ingress. `k8s/04b-multi-cluster-ingress.yaml` is a separately-applied `MultiClusterIngress` + `MultiClusterService` pair. | Enable Fleet MCI (`gcloud container fleet multi-cluster-ingress enable`); register both clusters as fleet members; apply the MCI manifest to the config cluster only. |
| 3. NGINX mTLS | `k8s/05-ingress-mtls-nginx.yaml` configures NGINX with `auth-tls-verify-client: on`; `k8s/10-secret-manager-sync.yaml` syncs the trusted CA from Secret Manager. | Install the ingress-nginx controller; seed the CA + partner cert in Secret Manager (see `docs/CERT_SETUP.md`); decide if the partner ingress is public or private. |

### What this repository does **not** configure

- Any Akamai property, delivery configuration, or purge token.
- Public DNS. The `demo.test` zone is a local Technitium demo only; the real `meridiansupply.com` zone is operator-managed.
- OAuth client registration for OIDC admin paths. The Terraform `GOOGLE_OIDC_AUDIENCE` is a placeholder; the actual client is created in the Google Cloud Console or via `gcloud iap oauth-clients create` out of band.
- The container images at `us-central1-docker.pkg.dev/PROJECT_ID_PLACEHOLDER/meridian-containers/...`. CI/CD (not in this repo) builds and pushes them.

## Regional traffic flip — what moves where

Three different "drain" operations exist. They sit at different layers and do different things:

| Mechanism | What it changes | Who runs it | What actually shifts traffic |
|---|---|---|---|
| **Method A: GCLB `capacity-scaler=0.0`** | Backend-service weight for a specific NEG set to 0.0. | SRE via `scripts/traffic_flip_manager.sh drain-central-gcp` or `.ps1`. | GCLB stops sending traffic to the drained NEG within one health-check window (~10-30 s, config-dependent). |
| **Method B: ServiceExport deletion (MCI)** | Removes the MCI-exported Service from a cluster. | SRE via `kubectl delete serviceexport ...`. | MCI-managed GCLB backends re-converge; failover depends on MCI reconcile time (typically 30-90 s). |
| **Method C: `DRAIN_REGION=true` ConfigMap** | Flips the application-level `region_state` row for a region; `/readyz` returns 503 within one `region_state` refresh cycle (~2 s per pod, pod-local cached). | SRE via `kubectl -n meridian-prod set env configmap/meridian-platform-config DRAIN_REGION=true` + restart, **or** operator POST to `/api/admin/drain` with an authenticated OIDC token. | GCLB/MCI/Akamai health-checker sees 503 → marks backend unhealthy → stops routing. Shift time = health-check interval × consecutive-fail threshold (GCE default: 10s × 3 = ~30s). |

**Important:** none of these mechanisms, by themselves, move 100% of global traffic. The traffic shift is a function of the *external router's* health-check configuration. "10-15 seconds" in older copy was the application-layer response; end-to-end cutover is bounded by the router.

## Verifying failover locally (no GCP required)

```bash
# Start the local stack:
./run-local.sh    # (or run-local.ps1)

# Confirm both regions are healthy:
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:8080/readyz
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:8082/readyz
# Expect: 200 and 200.

# Drain the primary (Method C via the Toggle Regional Readiness button, or):
TOKEN="<local admin token printed by run-local>"
curl -sS -X POST http://localhost:8080/api/admin/drain \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"drain":true}'

# Confirm the primary returns 503:
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:8080/readyz
# Expect: 503.

# Confirm the secondary stays healthy:
curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:8082/readyz
# Expect: 200.
```

Local Docker Compose does **not** emulate GCLB; the traffic shift between regions requires the external router. For a GCP verification run, see `docs/RUNBOOK_DR.md` and the Rubric & Verification Runbook PDF.

## Trust-boundary behavior for proxy headers

`True-Client-IP`, `X-Forwarded-For`, and `Akamai-Request-BC` are only trusted when the TCP peer sits inside `TRUSTED_UPSTREAM_CIDRS` (comma-separated env var). When unset (local dev), the headers are accepted. In production, operators must set this to the GCLB health-check and Akamai Site Shield ranges that terminate TLS in front of the pods. Pods that receive requests from arbitrary network peers will zero the client-IP header before persisting or logging.

See `apps/storefront-ui/app.py:_peer_is_trusted` and the equivalent in `apps/order-service/app.py`.
