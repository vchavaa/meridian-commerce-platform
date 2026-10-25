# Meridian Commerce Platform

Meridian Commerce is a reference implementation and deployment template for a Python storefront and order API. It includes a local Docker Compose topology, Kubernetes manifests, Terraform examples, and an offline simulator.

> **Deployment status:** This repository does not represent a live or verified GCP deployment. Cloud resources, DNS, certificates, Akamai configuration, external routing, and production secrets must be supplied and tested by the operator. See [Architecture and Design](./docs/ARCHITECTURE_AND_DESIGN.md) for implemented behavior and remaining boundaries, and the [Step-by-Step Guide](./docs/STEP_BY_STEP_GUIDE.md) before deployment.

## Components

- `apps/storefront-ui`: catalog storefront, checkout forwarding, health/readiness, and authenticated regional drain endpoint.
- `apps/order-service`: order creation, protected order listing, readiness/drain endpoint, and a transactional Pub/Sub outbox publisher.
- `apps/common`: shared OIDC/local bearer authentication, PostgreSQL access, regional readiness state, and bounded HTTP server.
- `docker-compose.yml`: PostgreSQL plus primary/secondary storefront and order-service containers for local functional testing.
- `k8s/`: GKE workloads, public storefront ingress with Cloud Armor configuration, separate partner ingress requiring mTLS, and network policies.
- `terraform/`: infrastructure examples for GKE, networking, Cloud SQL, Pub/Sub, IAM, and Cloud Armor.
- `scripts/`: local simulation and operator helper scripts.
- `observability/`: sample dashboards, queries, and simulation logs; these files do not indicate live telemetry.

## Run locally on Windows

Requirements: Docker Desktop with Docker Compose, Python 3.10+ available as `python`, and the local Python dependencies installed:

From PowerShell in the repository root:

```powershell
python -m pip install -r requirements-local.txt
.\run-local.ps1
```

The runner executes the offline simulator, generates a temporary local admin token, and starts the containers. A local secret-agent sidecar writes it to a restricted shared Docker volume; the sidecar generates and persists a PostgreSQL password in that volume so the local database can retain its data across runs. The runner prints the bearer token. Keep it private and use it only for this local run. The local endpoints are:

| Service | URL |
| --- | --- |
| Primary storefront | <http://localhost:8080> |
| Primary order console/API | <http://localhost:8081> |
| Secondary storefront | <http://localhost:8082> |
| Secondary order console/API | <http://localhost:8083> |

Press Ctrl+C to stop the runner and containers. The named PostgreSQL volume persists. Remove it only when you intentionally want to delete local order data:

```powershell
docker compose down --volumes
```

If upgrading a local database volume from an older runner version, its database password may not have been persisted. If the new local services cannot authenticate, back up any needed local data before intentionally recreating the local volumes with `docker compose down --volumes`.

On macOS/Linux, run `./run-local.sh`. For direct API checks and manual Compose start/stop instructions, see the [Step-by-Step Guide](./docs/STEP_BY_STEP_GUIDE.md).

## Authentication and data

- Local admin APIs accept `Authorization: Bearer <LOCAL_ADMIN_TOKEN>` only when `APP_ENV=local`. The runner generates the token for the current run.
- Production admin APIs verify Google OIDC ID tokens, the configured audience, verified email status, and the `ADMIN_ALLOWED_EMAILS` allowlist.
- `X-IAM-Role` is not an authentication mechanism.
- Order, idempotency, outbox, and regional drain state are stored in PostgreSQL. There is no in-memory success fallback when the database is unavailable.
- Pub/Sub outbox delivery is at least once. Consumers must deduplicate using event ID.
- Compose binds host ports to loopback and stores PostgreSQL data and its local-only database password in named Docker volumes.

## Architecture and operational boundaries

1. The public storefront is served through the GKE external Ingress, with Cloud Armor attached through its BackendConfig.
2. The partner API is a separate NGINX ingress that requires client-certificate validation. Install ingress-nginx and provision its TLS/CA Kubernetes Secrets through the GKE SecretSync configuration. Cloud Armor on the public GKE ingress does not automatically protect this separate endpoint.
3. The regional drain API updates PostgreSQL-backed readiness state. A readiness failure does not itself implement cross-region routing; configure and test global routing/Akamai failover separately.
4. Terraform configures regional Cloud SQL HA in the primary region. This does not provide cross-region database failover; design and test backup/restore or replication and define RTO/RPO.
5. Akamai Site Shield CIDRs and an origin secret are required Terraform inputs. The origin value is stored in Secret Manager, but Cloud Armor requires it in the policy expression; the Terraform state therefore contains sensitive policy data and must be protected. Configure Akamai to overwrite the origin-secret header.
6. The Compose setup simulates two region-labelled app pairs sharing one local database. It does not emulate separate regional databases, GKE ingress, Cloud Armor, mTLS termination, or global failover.

For detailed flows, security assumptions, scalability controls, and residual production work, read [Architecture and Design](./docs/ARCHITECTURE_AND_DESIGN.md). For external infrastructure prerequisites and operator triage, read the [Step-by-Step Guide](./docs/STEP_BY_STEP_GUIDE.md).

## Local Technitium DNS demo

The optional DNS example is independent of the app deployment. Create a `demo.test` Primary Zone in Technitium and add the records below to demonstrate a CNAME chain ending in a local A record:

| Name | Type | Value |
| --- | --- | --- |
| `www` | CNAME | `edge.akamai.demo.test` |
| `edge.akamai` | CNAME | `global-lb.demo.test` |
| `global-lb` | A | `127.0.0.1` |

Reuse an existing container if one already owns the name or ports. See the [DNS setup and troubleshooting steps](./docs/STEP_BY_STEP_GUIDE.md#5-optional-technitium-dns-demo). This does not configure public DNS, Akamai, or a GCP load balancer.

## Secrets and credential hygiene

Local Compose uses a secret-agent sidecar to stage the demo token and a generated database password in a Docker volume. The token is removed on clean shutdown; the database password remains alongside the persistent database volume and is removed only when that volume is intentionally deleted.

For GKE, Terraform provisions Secret Manager entries for ingress certificates replicated in both cluster regions and grants a dedicated Kubernetes workload identity access only to those certificate/CA secrets. GKE's Secret Manager synchronization controller maps those values to the Kubernetes TLS/CA Secrets expected by the ingress controllers. Secret payloads must be added out of band; Terraform does not store certificate bytes. Create the Akamai origin secret out of band before the first Terraform plan because Terraform reads it into the sensitive Cloud Armor policy input. Current production app authentication and Cloud SQL access use OIDC and IAM respectively, so app pods do not need shared admin/database passwords.

The Akamai origin value is held in Secret Manager and supplied to Terraform from a protected deployment environment. Cloud Armor embeds the comparison value in its policy expression, so Terraform state can contain the value even when the input is marked sensitive. Use encrypted remote state with strict access controls. No private certificate keys or production secret values belong in this repository. Certificate/key files and credentials previously present in the repository must be treated as compromised: revoke or rotate them and review Git history, clones, and caches. Removing files from the current checkout does not erase repository history.

## Validation

Run local verification with `python scripts/simulate_and_verify.py`, then exercise the Compose services using the runner. Terraform and Kubernetes manifests must be validated against the chosen GCP project, provider/controller versions, enabled APIs, IAM configuration, and cluster networking before deployment. No cloud apply or production deployment is performed by this project guide.
