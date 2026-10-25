# Architecture and Design

## 1. Scope and deployment status

Meridian Commerce is a reference implementation for a storefront, order API, and cloud deployment patterns. The repository contains application code, local Docker Compose configuration, Kubernetes manifests, and Terraform examples. It does not represent a live or validated GCP deployment. Domain names, project identifiers, IAM, TLS material, Akamai settings, quotas, and external routing must be configured by the operator.

The deployment examples target two GKE regions, but automatic cross-region traffic failover and cross-region database recovery are not implemented end to end. Treat availability claims as unverified until the external global router, Akamai configuration, and database recovery process have been configured and exercised.

## 2. Logical components

```text
Customer browser
      |
      | HTTPS (public storefront)
      v
GKE external Ingress + Cloud Armor
      |
      v
storefront-ui ---- private Kubernetes Service ----> order-service
                                                   |
                                                   +--> PostgreSQL / Cloud SQL
                                                           |
                                                           +--> transactional outbox
                                                                    |
                                                                    +--> Pub/Sub (optional)

B2B client -- client-certificate TLS --> separate ingress-nginx Ingress
                                            |
                                            +--> /api/orders
```

The public storefront ingress and partner API ingress are separate entry points. The public GKE ingress references the Cloud Armor BackendConfig. The NGINX partner ingress requires a client certificate and requires an independently installed ingress-nginx controller. Cloud Armor on the GKE ingress does not automatically protect the separate NGINX endpoint.

### Services

| Component | Responsibility | Important behavior |
| --- | --- | --- |
| `storefront-ui` | Catalog, storefront HTML, checkout forwarding, readiness and administrative drain | Bounded HTTP server; forwards checkout to the configured order API; does not own order persistence |
| `order-service` | Order creation, order ledger, readiness and administrative drain | PostgreSQL transactions persist orders, idempotency keys, and outbox events |
| PostgreSQL | Durable application state | Local Compose uses one persistent named volume; the Terraform example creates one regional-HA Cloud SQL PostgreSQL instance |
| Pub/Sub publisher | Publishes committed order events | Reads the transactional outbox and publishes at least once when `PUBSUB_TOPIC` is configured |
| ingress-nginx | Partner API TLS termination and client-certificate verification | Must be installed separately; its CA/TLS Secrets are provisioned by the operator |

The two Compose storefront/order pairs model primary and secondary service endpoints, but both pairs share one local PostgreSQL instance. This is a demo topology, not an independent-region failure test.

The optional advanced-routing manifest creates a separate internal HTTP GKE Gateway at `advanced-routing.meridiansupply.com`; it is not the public HTTPS Ingress. It requires the GKE Gateway controller/API and a private DNS entry for the provisioned internal address. The stable storefront Service selects only stable pods, while the canary Service selects only canary pods. Do not expose this Gateway publicly or treat it as the production edge without configuring TLS, policy enforcement, and environment-specific routing.

## 3. Request and authorization flows

### Public storefront and checkout

1. A browser connects to the public storefront hostname over HTTPS.
2. The GKE external Ingress routes to `storefront-ui`; its BackendConfig attaches the configured Cloud Armor policy.
3. Checkout is forwarded from `storefront-ui` to `order-service` over the in-cluster service address.
4. The order service validates request data and records the order, idempotency key, and outbox event in a PostgreSQL transaction.
5. A configured publisher claims pending outbox events and publishes them to Pub/Sub. Delivery is at least once; consumers must deduplicate by event ID.

The repository does not configure the Akamai property, DNS, global multi-cluster routing, or origin selection. Configure the origin secret header to be overwritten at the edge and validate Site Shield CIDRs with Akamai. The Cloud Armor rule protects only the backend to which it is attached.

### Partner mTLS

The partner hostname routes through ingress-nginx to `/api/orders`. NGINX performs TLS client-certificate validation using `ingress-ca-secret`. It does not forward the certificate to the application, and application code ignores client-certificate identity headers; mTLS is an ingress access gate, not an application identity assertion.

The order service remains reachable to storefront pods for checkout, so it must not infer partner identity from request headers. Its NetworkPolicy permits the storefront and only ingress-nginx controller pods labeled `app.kubernetes.io/name=ingress-nginx,app.kubernetes.io/component=controller`; ensure the installed controller uses those labels and that the cluster dataplane enforces NetworkPolicies. If partner identity is later required for authorization or auditing, add a separate authenticated ingress-to-service identity mechanism and explicit identity-to-permission mapping; do not re-enable trust in a caller-supplied certificate header.

### Operator APIs

- Production mode verifies Google OIDC ID tokens, including the configured audience, verified email, and `ADMIN_ALLOWED_EMAILS` allowlist.
- Local mode accepts `LOCAL_ADMIN_TOKEN` only when `APP_ENV=local`.
- The caller-controlled `X-IAM-Role` header is not accepted for authorization.
- Order listing and regional drain operations require authentication.

ID tokens should be minted by the operator's approved identity workflow. Do not put bearer tokens in URLs or logs.

## 4. Persistence and consistency

Order creation, idempotency, and the outbox event are committed within one PostgreSQL transaction. This prevents a committed order from being separated from the record of its event. The publisher claims work with a lease and records publication state; process crashes and retries can still cause duplicate messages after a successful Pub/Sub publish but before the database is updated. Consumers must use the event ID as an idempotency key.

Drain/readiness state is also stored in PostgreSQL and briefly cached by each process. Replicas therefore observe shared state without relying on pod-local memory, subject to the cache refresh interval. If PostgreSQL becomes unavailable, the state helper logs the failure and uses the last cached value (or configured default if there is no cached value); readiness is not guaranteed to become unhealthy solely because the state refresh failed. Order writes fail explicitly when PostgreSQL is unavailable. Monitor database connectivity and drain-state refresh errors, and treat the cache behavior as a bounded stale-read tradeoff.

### Local versus cloud database

- Docker Compose runs PostgreSQL in a named volume. `docker compose down` retains data; `docker compose down --volumes` removes it.
- Each app process uses a bounded PostgreSQL connection pool (`DB_POOL_MAX_SIZE`, default 4). Set pool size and replica/HPA limits together so their aggregate connection ceiling stays within the database's capacity.
- Kubernetes manifests use a Cloud SQL Auth Proxy sidecar with IAM database authentication. The configured instance name, database, IAM database principal, service-account binding, and connector arguments must match the deployed Cloud SQL instance and supported proxy version. Cloud SQL is configured with public IPv4 and SSL required; restrict authorized networks and review whether private-IP-only access is required. The Auth Proxy configuration does not itself disable direct connections.
- Terraform configures regional Cloud SQL HA, scheduled backups stored in `var.secondary_region`, and seven days of point-in-time recovery logs. Cloud SQL can restore a backup to a new instance in another region. The 60-minute RPO/RTO values used by the local drill are evaluation targets, not service guarantees: actual backup availability, recoverable point, restore duration, and application cutover must be measured in GCP.

#### Cross-region backup restore drill

Run `python scripts/simulate_disaster_recovery.py` for a deterministic, offline recovery-workflow evaluation. The simulation models primary-write fencing, selection of the newest secondary-region recovery point, restore, verification, and application cutover, and fails when the configured 60-minute RPO or RTO target is exceeded. It does not contact GCP, create a backup, restore a database, or change traffic.

For a real incident, first fence writes to the failed primary to prevent split-brain. Restore a recent Cloud SQL backup or point-in-time recovery into a **new** instance in the secondary region, verify the recovered data and application writes, update `CLOUD_SQL_INSTANCE_CONNECTION_NAME` in the workload ConfigMap and roll out both services, then shift traffic only after health checks pass. Keep the original primary fenced until a separately planned reconciliation/rebuild. The local drill does not automate or validate these GCP control-plane steps; test them in a non-production project and measure actual RPO/RTO before making availability claims.

The sample order endpoint is not a production commerce workflow: catalog prices/stock are static, and it does not reserve inventory, collect payment, or bind orders to authenticated customers. Treat it as a functional demo until authoritative pricing, inventory, customer identity, payment processing, and order-state transitions are designed and implemented.

## 5. Runtime and scalability controls

The Python HTTP servers use a bounded worker count and request queue, socket timeouts, and request body size limits. Overload is rejected rather than allowing unbounded thread growth. Kubernetes deployments define health probes, CPU autoscaling, pod disruption budgets, topology spread, and resource/security contexts.

These controls are starting points rather than capacity evidence. Load-test realistic checkout traffic, database pool/connection limits, outbox publisher throughput, queue saturation, CPU/memory requests, and HPA behavior. Define latency/error SLOs and alert thresholds using measured results. Avoid inferring service latency or resource utilization from static/demo telemetry.

## 6. Infrastructure and network boundaries

Terraform examples define VPC/subnets, GKE clusters, workload identity, Artifact Registry, Cloud SQL, Pub/Sub, Cloud Armor, and logging resources. Kubernetes manifests define the namespace, configuration, services, deployments, ingress, network policies, and RBAC examples.

Operational assumptions requiring verification:

- GKE must support and enforce the declared NetworkPolicies; validate the selected dataplane/provider and rules.
- The GKE ingress must successfully attach its BackendConfig and Cloud Armor policy to the intended storefront backend.
- Partner ingress requires a separately installed ingress-nginx controller with a controlled public/private exposure decision.
- GKE SecretSync provisions the public TLS Secret, partner TLS Secret, and trusted client CA Secret from Secret Manager. Enable secret synchronization and rotation on each GKE cluster, renew versions before expiry, and verify synchronization behavior in staging.
- Placeholder project, identity, image, audience, hostname, and instance values must be replaced consistently in Terraform and Kubernetes configuration.
- Terraform state can contain sensitive policy data; use a private encrypted backend, strict IAM, locking, and state retention controls.

## 7. Observability and operational readiness

Applications emit structured JSON logs to standard output. GKE can route container logs to Cloud Logging, while Terraform includes example logging sinks and Pub/Sub resources. The local simulator and checked-in dashboard/SQL artifacts are demonstrations; they are not proof of live GCP telemetry, log retention, alert delivery, or dashboard connectivity.

Before production use, configure and test:

- dashboards and alerts for request rate, errors, latency, saturation, pod restarts, database connections, outbox age/failures, and certificate expiration;
- trace propagation from ingress through checkout and order persistence;
- runbooks for failed readiness, Cloud SQL connectivity, Pub/Sub backlog, bad deployment, suspected credential exposure, and regional outage;
- log access controls, retention, redaction of personal/customer data, and audit retention;
- a release rollback process and an incident communication/escalation path.

The regional drain endpoint updates readiness state; it does not implement global traffic movement. Test external routing separately and define a rollback procedure before a regional exercise.

## 8. Security design and residual work

Implemented controls include OIDC-based operator authentication, local-only bearer-token authentication, a local Compose secret-agent sidecar, GKE SecretSync-based ingress certificate synchronization with a dedicated Kubernetes workload identity, PostgreSQL persistence instead of an in-memory order fallback, IAM-based Cloud SQL connectivity in Kubernetes, non-root application containers, restricted pod security settings, bounded HTTP workers/requests, an ingress-level mTLS example, and Kubernetes NetworkPolicy manifests.

Remaining deployment/security work:

1. Rotate/revoke any secrets and certificates that were ever committed. Removing a file from the current working tree does not purge repository history, forks, caches, or clones.
2. Populate the Terraform-provisioned Secret Manager secrets out of band, restrict secret IAM and Terraform state access, and ensure no credentials appear in process output, CI logs, or application logs. Cloud Armor policy state necessarily contains the origin-header comparison value.
3. Configure actual Akamai Site Shield ranges and a strong origin secret; verify Akamai overwrites the header and the Cloud Armor policy is attached to the intended backend.
4. Decide whether the partner ingress should be public or private and ensure the partner path has appropriate network/WAF controls independent of the public ingress.
5. Bind partner certificates to explicit partner identities/permissions, and plan issuance, renewal, revocation, expiry monitoring, and emergency CA replacement.
6. Review IAM roles, Kubernetes RBAC, firewall rules, egress, network policies, container scanning, dependency update process, and audit logs for the target environment.
7. Restrict Cloud SQL network reachability and confirm direct connections cannot bypass expected proxy/network controls.
8. Establish tested cross-region database recovery and global routing before claiming regional disaster recovery.

## 9. Local topology

`docker-compose.yml` starts a PostgreSQL service, two storefronts, two order services, and a local secret-agent sidecar. The runner generates a temporary admin token; the sidecar creates and retains a database password in the local secret volume. Both the database data volume and secret volume persist until explicitly removed. Host ports bind to loopback:

| Host port | Local service |
| --- | --- |
| `8080` | Primary storefront |
| `8081` | Primary order service |
| `8082` | Secondary storefront |
| `8083` | Secondary order service |

This setup is useful for functional testing of persistence, authentication, checkout, and local regional state. It does not emulate separate Cloud SQL instances, GKE ingress, mTLS termination, Akamai, Cloud Armor, or global failover.
