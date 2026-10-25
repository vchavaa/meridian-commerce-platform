# Step-by-Step Setup Guide

This guide covers local execution and the prerequisites for a future GCP deployment. Cloud resources are not deployed or tested by this repository. Do not use old deployment snippets or apply Terraform without reviewing the plan, costs, security configuration, and state backend.

## 1. Run locally on Windows

Prerequisites: Docker Desktop with the Compose plugin and Python 3.10 or newer available as `python`. Install the host-side simulator and service dependencies once:

From PowerShell at the repository root:

```powershell
python -m pip install -r requirements-local.txt
.\run-local.ps1
```

The script runs the offline simulator, generates a temporary admin token, builds the containers, and starts the local Compose topology. The secret-agent sidecar stages the token and a generated PostgreSQL password in a Docker volume; the database password persists with the named database volume so existing local data remains usable on the next run. The admin token is printed for local operations; keep it private and use it only during this run. Open:

| Endpoint | URL |
| --- | --- |
| Primary storefront | <http://localhost:8080> |
| Primary order console | <http://localhost:8081> |
| Secondary storefront | <http://localhost:8082> |
| Secondary order console | <http://localhost:8083> |

Press Ctrl+C to stop the script. It runs `docker compose down`; the named PostgreSQL and local secret volumes remain. The sidecar removes the local admin token on clean shutdown. To remove the local database and its password intentionally, use `docker compose down --volumes`.

When upgrading a database volume created by the older runner, the former randomly generated database password might not be recoverable. If the app cannot authenticate to PostgreSQL, back up any data you need before intentionally recreating the local volumes with `docker compose down --volumes`.

### Start Compose manually

If you do not use the runner, generate an admin token in the same PowerShell session and pass it to Compose. The sidecar creates the local database password on first start and persists it with the local volumes:

```powershell
$env:LOCAL_ADMIN_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
docker compose up --build --detach
docker compose ps
```

After testing:

```powershell
docker compose down
Remove-Item Env:\LOCAL_ADMIN_TOKEN
```

The services expose ports only on loopback. Orders and regional drain state persist in the PostgreSQL volume. Use `docker compose logs -f` for service logs.

## 2. Run locally on macOS or Linux

Install the local requirements above, then run this from the repository root:

```bash
python3 -m pip install -r requirements-local.txt
./run-local.sh
```

The script runs the same simulator and local Compose topology, generates temporary credentials, and removes the containers when stopped. The PostgreSQL named volume persists unless explicitly removed with `docker compose down --volumes`.

## 3. Local authentication and API checks

The local runner prints `LOCAL_ADMIN_TOKEN`. Supply it as a bearer token for admin routes and protected order listing; do not use the obsolete `X-IAM-Role` header:

```powershell
$headers = @{ Authorization = "Bearer $env:LOCAL_ADMIN_TOKEN" }
Invoke-RestMethod -Method Get -Uri "http://localhost:8080/api/catalog"
Invoke-RestMethod -Method Get -Uri "http://localhost:8081/api/orders" -Headers $headers
```

The local token is accepted only in `APP_ENV=local`. In a cloud environment, admin routes require a Google OIDC ID token for the configured audience and an email in `ADMIN_ALLOWED_EMAILS`. Never place production tokens or secrets in source control.

## 4. Optional offline simulator

Run `python scripts/simulate_and_verify.py` to run the repository's offline simulation independently. It is not a deployment check and does not query Cloud Logging, BigQuery, GCP, Akamai, or a live DNS service. The Jupyter notebook is a separate demonstration; its output should not be treated as live production telemetry.

## 5. Optional Technitium DNS demo

The DNS demo is independent of the commerce services and does not configure Akamai or GCP. Reuse an existing Technitium container if one is already running:

```powershell
docker ps -a --filter "name=meridian-technitium-dns"
docker port meridian-technitium-dns
```

If creating a new instance, ensure the required ports are available and configure an admin password:

```powershell
docker run -d `
  --name meridian-technitium-dns `
  -p 5380:5380/tcp `
  -p 53:53/udp `
  -p 53:53/tcp `
  -e DNS_SERVER_DOMAIN="ns1.demo.test" `
  -e DNS_SERVER_ADMIN_PASSWORD="<choose-a-strong-local-password>" `
  technitium/dns-server:latest
```

In Technitium, create a Primary Zone `demo.test` and add:

| Name | Type | Value |
| --- | --- | --- |
| `www` | CNAME | `edge.akamai.demo.test` |
| `edge.akamai` | CNAME | `global-lb.demo.test` |
| `global-lb` | A | `127.0.0.1` |

Then verify the DNS records:

```powershell
nslookup www.demo.test 127.0.0.1
Resolve-DnsName -Name www.demo.test -Server 127.0.0.1 -Type A
```

If the answer is `NXDOMAIN`, confirm the zone and all three records were saved. `nslookup` may show the final A answer rather than every CNAME in the chain; query individual CNAMEs in Technitium's DNS Client.

## 6. GCP deployment preparation (operator action; not performed)

Cloud deployment requires project-specific review and external configuration. The repository's Terraform and Kubernetes manifests are templates, not a one-command production rollout. Before creating resources:

1. Choose the project, regions, domain names, service quotas, and supported GKE/network-policy configuration. Review all Terraform resources and estimate recurring Cloud SQL, GKE, networking, Artifact Registry, logging, and Pub/Sub costs.
2. Configure a private, encrypted Terraform remote backend with restricted access and state locking. Terraform state contains the Cloud Armor origin-secret policy expression and must be protected. Do not store production secrets in `terraform.tfvars`.
3. Supply current Akamai Site Shield CIDRs. Before the first Terraform plan, create the `meridian-akamai-origin-secret` Secret Manager secret and add its first version from a protected operator environment (subsequent rotations add another version):

   ```powershell
   gcloud secrets create meridian-akamai-origin-secret --project <project-id> --replication-policy=automatic
   gcloud secrets versions add meridian-akamai-origin-secret --project <project-id> --data-file=<secure-file>
   .\scripts\load-akamai-origin-secret.ps1 -ProjectId <project-id>
   terraform plan
   terraform apply
   Remove-Item Env:\TF_VAR_akamai_origin_secret
   ```

   The deployment identity needs Secret Manager access to this secret. The resulting Cloud Armor expression is persisted in Terraform state. Configure Akamai to overwrite the origin header. Do not use sample/static secrets or stale CIDRs.
4. Configure GCP IAM and Workload Identity, Cloud SQL IAM database access, Artifact Registry, API enablement, and Cloud SQL connector identity. Review and validate Terraform provider/API compatibility against the selected versions. The current Cloud SQL template enables public IPv4 and requires SSL; restrict network access or adapt it to private IP as appropriate—do not assume the Auth Proxy setting disables direct connections.
5. Set real project, Cloud SQL instance, order topic, OIDC audience, and verified operator email allowlist values in the Kubernetes configuration. Replace image references. Do not apply the placeholder email/audience values.
6. Terraform creates Secret Manager metadata for the public TLS cert/key, partner TLS cert/key, and partner client CA with replicas in both cluster regions; it does not create secret versions or embed their bytes. Add the certificate/key/CA values using `gcloud secrets versions add <secret-id> --data-file=<secure-file>` from a secured operator environment. GKE SecretSync requires GKE 1.33 or later; Terraform sets that minimum. After Terraform creates both clusters, enable native secret synchronization and two-minute rotation:

   ```powershell
   .\scripts\enable-gke-secret-sync.ps1 -ProjectId <project-id> -PrimaryLocation <zone-or-region> -SecondaryLocation <zone-or-region>
   ```

   For each cluster, replace the example project ID in `k8s/01-namespace-and-config.yaml` and `k8s/10-secret-manager-sync.yaml`, then apply both manifests. Confirm the `meridian-public-tls`, `meridian-partner-tls`, and `ingress-ca-secret` Kubernetes Secrets exist before applying their Ingress resources. Install/configure ingress-nginx for the partner endpoint. Never put private keys or secret payloads in the repository or command history.
7. Validate manifests and network policies in a staging cluster. Verify the Cloud Armor attachment applies to the public storefront backend; it does not protect the separate NGINX partner endpoint.
8. Configure an actual global multi-cluster traffic manager and Akamai failover policy. The readiness drain alone does not shift traffic between clusters. Design and test a cross-region database recovery strategy; Cloud SQL regional HA only covers the configured region.
9. Run Terraform formatting, initialization, validation and a reviewed `terraform plan`. Apply only after explicit operator approval; then build and push images from the repository root because Dockerfiles use root build contexts:

   ```powershell
   docker build -f apps/storefront-ui/Dockerfile -t <registry>/storefront-ui:<tag> .
   docker build -f apps/order-service/Dockerfile -t <registry>/order-service:<tag> .
   ```

No deployment command is provided here because the project, external secrets, live certificate setup, and global routing design are deployment decisions that must be confirmed and tested by the operator.

## 7. Operational verification and triage

- **Application health:** check `/healthz` for process health and `/readyz` for regional drain state. `/readyz` does not directly validate order-database writes; test database access through the order API and inspect proxy/database logs.
- **Database:** verify Cloud SQL Auth Proxy readiness, IAM database authentication, connection saturation, failed transactions, and the oldest pending/failed outbox event.
- **Authorization:** confirm OIDC audience, token expiry, verified email and allowlist. Never troubleshoot by reintroducing a caller-controlled role header.
- **Partner mTLS:** check ingress-nginx controller health, TLS Secret validity, CA trust chain, client certificate expiry/revocation, and controller configuration. A browser request without a client certificate is expected to fail. The app deliberately does not receive or trust the ingress-validated certificate as an application identity.
- **Public origin:** verify current Akamai Site Shield source ranges, the Cloud Armor policy attachment, and origin-header overwrite. Do not expose the origin directly to the internet while bypassing expected protections.
- **Regional failover:** treat drain as a readiness change only. Verify external routing separately and conduct controlled failover tests with a rollback plan.
- **Incident response:** preserve relevant logs and trace IDs, identify affected region/service and change window, check recent deployment/configuration changes, and document mitigation plus follow-up actions.
