# Certificate Setup for NGINX Partner mTLS Ingress

**Scope:** how an operator produces the public TLS, partner TLS, and trusted-client-CA material that `k8s/10-secret-manager-sync.yaml` syncs from Google Secret Manager into the cluster `ingress-ca-secret`, `meridian-public-tls`, and `meridian-partner-tls` secrets.

**Non-goal:** this repository deliberately does **not** commit any CA key, server key, or client key material. The PDFs' file-tree diagrams that previously showed a `certs/` directory were incorrect — see `docs/SECRETS_ROTATION.md` for the rotation note.

---

## 1. Produce a Root CA for partner mTLS

For development / demo:

```bash
mkdir -p /tmp/meridian-ca && cd /tmp/meridian-ca

openssl genrsa -out ca.key 4096

openssl req -x509 -new -nodes -key ca.key \
  -subj "/CN=Meridian-Ingress-Root-CA/O=Meridian Supply Co." \
  -days 3650 -sha256 -out ca.crt
```

For production: use Google Cloud **Certificate Authority Service** (`gcloud privateca ...`) with an HSM-backed key and rotation policy, not an `openssl`-local CA.

---

## 2. Issue a B2B partner client certificate

```bash
openssl genrsa -out client.key 2048

openssl req -new -key client.key \
  -subj "/CN=b2b-partner-client/O=Meridian-Partner" \
  -out client.csr

openssl x509 -req -in client.csr \
  -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 365 -sha256 -out client.crt
```

Keep `ca.key` offline. Only `ca.crt` ever leaves the issuing host (and only as part of the trust bundle).

---

## 3. Produce the public and partner server certificates

For `www.meridiansupply.com` (public) and `partner.meridiansupply.com` (partner API):

```bash
# Public TLS:
openssl req -newkey rsa:2048 -nodes \
  -keyout public.key -out public.csr \
  -subj "/CN=www.meridiansupply.com"

# For production, replace with a managed cert from Google Certificate Manager
# or Let's Encrypt via cert-manager; the Secret pattern below is unchanged.

# Partner TLS (server-side cert; the client cert from Step 2 goes on the partner):
openssl req -newkey rsa:2048 -nodes \
  -keyout partner.key -out partner.csr \
  -subj "/CN=partner.meridiansupply.com"
```

---

## 4. Upload to Google Secret Manager

The GKE Secret-Manager Sync (CSI driver) in `k8s/10-secret-manager-sync.yaml` reads these five Secret Manager resources:

| Secret Manager resource | Contents |
|---|---|
| `meridian-public-tls-cert` | public server cert PEM |
| `meridian-public-tls-key` | public server key PEM |
| `meridian-partner-tls-cert` | partner server cert PEM |
| `meridian-partner-tls-key` | partner server key PEM |
| `meridian-partner-client-ca` | root CA cert PEM (public portion only) |

Create and populate them:

```bash
export PROJECT_ID="<your-project-id>"

for name in meridian-public-tls-cert meridian-public-tls-key \
            meridian-partner-tls-cert meridian-partner-tls-key \
            meridian-partner-client-ca; do
  gcloud secrets create "$name" --project="$PROJECT_ID" --replication-policy=automatic || true
done

# Example uploads:
gcloud secrets versions add meridian-public-tls-cert --data-file=public.crt
gcloud secrets versions add meridian-public-tls-key  --data-file=public.key
gcloud secrets versions add meridian-partner-tls-cert --data-file=partner.crt
gcloud secrets versions add meridian-partner-tls-key  --data-file=partner.key
gcloud secrets versions add meridian-partner-client-ca --data-file=ca.crt
```

Grant the GKE Workload Identity SA used by the CSI driver access to each secret:

```bash
export SECRET_SYNC_SA="meridian-secret-sync@${PROJECT_ID}.iam.gserviceaccount.com"

for name in meridian-public-tls-cert meridian-public-tls-key \
            meridian-partner-tls-cert meridian-partner-tls-key \
            meridian-partner-client-ca; do
  gcloud secrets add-iam-policy-binding "$name" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${SECRET_SYNC_SA}" \
    --role=roles/secretmanager.secretAccessor
done
```

---

## 5. Install the Secrets Store CSI driver and apply manifests

```bash
# One-time per cluster:
gcloud container clusters update meridian-gke-primary \
  --project="$PROJECT_ID" --region=us-central1 \
  --update-addons=GcpSecretManagerCSIDriver=ENABLED

kubectl apply -f k8s/10-secret-manager-sync.yaml
```

The driver creates and refreshes `ingress-ca-secret`, `meridian-public-tls`, and `meridian-partner-tls` Kubernetes Secrets from Secret Manager.

---

## 6. Verify mTLS end-to-end

```bash
# Public path (no client cert required):
curl -I https://www.meridiansupply.com/ -H "Host: www.meridiansupply.com"
# Expect: HTTP/2 200

# Partner path (client cert required):
curl -I https://partner.meridiansupply.com/api/orders \
  --cert client.crt --key client.key \
  -H "Host: partner.meridiansupply.com"
# Expect: HTTP/2 405 (POST required) - but no TLS error.

# Partner path without a client cert:
curl -I https://partner.meridiansupply.com/api/orders -H "Host: partner.meridiansupply.com"
# Expect: TLS error "No required SSL certificate was sent" (400).
```

The ingress logs `Ssl-Client-Subject-Dn` for audit. Application code does **not** use it as an authorization decision — mTLS is an access gate; identity/permissions are bound at a dedicated auth layer (see `docs/ARCHITECTURE_AND_DESIGN.md` §3).

---

## 7. Rotation

- CA cert: 1y validity typical; rotate before expiry; both old and new accepted during overlap window.
- Partner client certs: 90–180 d; distribute new cert to partner before expiry; revoke via CA revocation list.
- Server TLS: managed cert auto-rotation preferred.
- Any key that leaves its original host (via Slack, email, repo, screenshot): rotate immediately. See `docs/SECRETS_ROTATION.md`.
