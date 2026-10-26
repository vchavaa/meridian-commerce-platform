# Disaster Recovery Runbook — Cross-Region Cloud SQL Restore

**Status:** operator runbook. Not an automated procedure. Dry-run in a non-production project before using in prod.
**Scope:** failure of the Cloud SQL primary in `us-central1`; restore into a new instance in `us-east1`; cut application traffic over.
**Owner:** SRE on-call.
**Related:**
- `scripts/simulate_disaster_recovery.py` — offline tabletop that checks phase-duration math; does **not** execute these steps.
- `docs/ARCHITECTURE_AND_DESIGN.md` §4 — persistence and consistency model.

## Targets (operator-set, not guarantees)

| Target | Default | Where set |
|---|---|---|
| RPO (max data loss) | 15 min | measured as the age of the newest usable backup / PITR point |
| RTO (max time to serve traffic) | 60 min | measured end-to-end from decision to cutover |

Both must be re-measured against the actual GCP project after the first live rehearsal and re-measured yearly. The simulator's numbers are **not** measurements.

---

## 0. Preconditions

- You have `roles/cloudsql.admin`, `roles/compute.loadBalancerAdmin`, `roles/container.admin`, and `roles/secretmanager.admin` on the project.
- `gcloud auth login` and `gcloud config set project $PROJECT_ID` are current.
- The failure has been triaged and declared an incident. The incident commander has approved cross-region cutover.
- A paging channel is open and a scribe is appointed.

```bash
export PROJECT_ID="<your-project-id>"
export PRIMARY_REGION="us-central1"
export SECONDARY_REGION="us-east1"
export SOURCE_INSTANCE="meridian-orders-db"
export NEW_INSTANCE="meridian-orders-db-dr-$(date +%Y%m%d-%H%M)"
export DB_NAME="meridian"
```

---

## 1. Fence writes to the broken primary

Prevent split-brain while the recovery runs. Choose one of:

### 1a. Readiness drain (shuts the app's write path)

```bash
# From any shell with cluster credentials:
kubectl -n meridian-prod set env configmap/meridian-platform-config DRAIN_REGION=true
kubectl -n meridian-prod rollout restart deployment/storefront-ui deployment/order-service

# Verify every pod reports 503 on /readyz within one state-refresh cycle (~2s):
for region in us-central1 us-east1; do
  kubectl --context "gke_${PROJECT_ID}_${region}_meridian-gke-${region/us-/}" \
    -n meridian-prod exec deploy/storefront-ui -- \
    curl -fsS -o /dev/null -w "%{http_code}\n" http://localhost:8080/readyz
done
# Expect: 503 503
```

### 1b. Firewall fence (hard stop at the network)

Use when the application control plane is unreachable:

```bash
gcloud compute firewall-rules create meridian-fence-primary-sql \
  --project="$PROJECT_ID" \
  --network=meridian-prod-vpc \
  --direction=INGRESS \
  --action=DENY --rules=tcp:5432,tcp:3307 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=gke-meridian-node \
  --priority=100
```

Delete this rule only after cutover to the DR instance is confirmed healthy.

---

## 2. Identify the newest usable recovery point

```bash
# List backups, newest first:
gcloud sql backups list --instance="$SOURCE_INSTANCE" --sort-by=~windowStartTime --limit=10

# Pick the newest 'SUCCESSFUL' backup. Record its ID:
export BACKUP_ID="<backup_id_from_above>"

# Confirm backup region (must be or replicate to secondary region):
gcloud sql backups describe "$BACKUP_ID" --instance="$SOURCE_INSTANCE" --format=json \
  | jq '{id, startTime, status, kind, location}'
```

If no backup satisfies RPO, consider PITR to a specific timestamp:

```bash
gcloud sql instances describe "$SOURCE_INSTANCE" --format='value(settings.backupConfiguration.pointInTimeRecoveryEnabled)'
# Expect: True

# Choose a target timestamp (ISO 8601, UTC); must be inside the retention window:
export PITR_TS="$(date -u -d '-10 minutes' +%Y-%m-%dT%H:%M:%SZ)"
```

**Decision gate:** write the chosen backup ID or PITR timestamp into the incident doc before continuing.

---

## 3. Restore into a new instance in the secondary region

Cloud SQL restores to a **new** instance; never restore over a running one.

```bash
# Option A - restore from backup:
gcloud sql instances clone "$SOURCE_INSTANCE" "$NEW_INSTANCE" \
  --project="$PROJECT_ID" \
  --region="$SECONDARY_REGION" \
  --backup-id="$BACKUP_ID"

# Option B - restore to a point in time:
gcloud sql instances clone "$SOURCE_INSTANCE" "$NEW_INSTANCE" \
  --project="$PROJECT_ID" \
  --region="$SECONDARY_REGION" \
  --point-in-time="$PITR_TS"

# Promote to regional HA + enable IAM auth + require SSL:
gcloud sql instances patch "$NEW_INSTANCE" \
  --availability-type=REGIONAL \
  --database-flags=cloudsql.iam_authentication=on \
  --require-ssl

# Wait for RUNNABLE:
while [ "$(gcloud sql instances describe "$NEW_INSTANCE" --format='value(state)')" != "RUNNABLE" ]; do
  echo "waiting for $NEW_INSTANCE..."; sleep 20
done
```

---

## 4. Verify the recovered data

Open a Cloud SQL Studio session or `gcloud sql connect` and run:

```sql
-- Row counts and newest ids per table:
SELECT 'orders' AS t, COUNT(*) AS rows, MAX(id) AS max_id FROM orders
UNION ALL
SELECT 'idempotency_keys', COUNT(*), MAX(key) FROM idempotency_keys
UNION ALL
SELECT 'order_outbox', COUNT(*), MAX(id) FROM order_outbox;

-- Latest order timestamp to check RPO:
SELECT MAX(created_at) AS newest_order FROM orders;

-- Un-published outbox events (publisher will reprocess at least once; consumers
-- must deduplicate by event_id - see docs/ARCHITECTURE_AND_DESIGN.md §4):
SELECT COUNT(*) AS unpublished FROM order_outbox WHERE published_at IS NULL;
```

Decision gate: compare `newest_order` timestamp to RPO target. If stale beyond RPO, escalate before cutover.

---

## 5. Grant the Workload Identity service account access to the new instance

```bash
# Service-account binding on the new Cloud SQL instance:
gcloud sql users create "meridian-app-workload-sa@${PROJECT_ID}.iam" \
  --instance="$NEW_INSTANCE" \
  --type=cloud_iam_service_account

# Grant the Workload Identity SA access to connect:
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:meridian-app-workload-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/cloudsql.client
```

Confirm existing bindings didn't disappear during the clone.

---

## 6. Cut application traffic to the new instance

### 6a. Update the ConfigMap

```bash
export NEW_CONN="${PROJECT_ID}:${SECONDARY_REGION}:${NEW_INSTANCE}"

kubectl -n meridian-prod patch configmap meridian-platform-config \
  --type merge \
  -p "$(cat <<EOF
{"data":{"CLOUD_SQL_INSTANCE_CONNECTION_NAME":"$NEW_CONN","DRAIN_REGION":"false"}}
EOF
)"

kubectl -n meridian-prod rollout restart deployment/storefront-ui deployment/order-service
kubectl -n meridian-prod rollout status deployment/storefront-ui --timeout=180s
kubectl -n meridian-prod rollout status deployment/order-service --timeout=180s
```

### 6b. Verify readiness returns 200

```bash
for region in us-central1 us-east1; do
  kubectl --context "gke_${PROJECT_ID}_${region}_meridian-gke-${region/us-/}" \
    -n meridian-prod exec deploy/storefront-ui -- \
    curl -fsS -o /dev/null -w "%{http_code}\n" http://localhost:8080/readyz
done
# Expect: 200 200
```

### 6c. Verify a canary write + read against the new DB

```bash
# From a secure jump host (not from a public endpoint):
curl -X POST https://www.meridiansupply.com/api/checkout \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: dr-verify-$(uuidgen)" \
  -d '{"sku":"MRD-412","quantity":1}'
# Expect HTTP 200 with an ORD-... id, and the order visible in Studio.
```

### 6d. Confirm the external router is sending traffic

```bash
# GCLB backend-service health:
gcloud compute backend-services get-health <backend-service-name> --global
# Expect all NEG endpoints HEALTHY.

# Akamai (if configured) origin check panel - manual confirmation.
```

---

## 7. Monitor and verify

- Grafana / Cloud Monitoring: `httpRequest.status` 5xx rate below alert threshold for 10 min.
- Pub/Sub `meridian-order-events` consumer backlog decreasing.
- `order_outbox.published_at` catching up (duplicate consumption expected; event_id is the dedup key).
- No `region_state` DB-refresh error logs after cutover.

If any of these fail: pause, do not roll forward, consider the rollback below.

---

## 8. Rollback (if cutover fails)

```bash
# Revert ConfigMap to the primary connection name:
kubectl -n meridian-prod patch configmap meridian-platform-config \
  --type merge \
  -p "$(cat <<EOF
{"data":{"CLOUD_SQL_INSTANCE_CONNECTION_NAME":"${PROJECT_ID}:${PRIMARY_REGION}:${SOURCE_INSTANCE}","DRAIN_REGION":"true"}}
EOF
)"

# Fence remains in place from Step 1 until decision is final.
kubectl -n meridian-prod rollout restart deployment/storefront-ui deployment/order-service
```

Keep the new instance paused (don't delete) until root cause is understood.

---

## 9. Post-incident

1. **Do not delete** `$NEW_INSTANCE` for at least 7 days; it is forensic evidence.
2. **Do not** reconnect writes to the old primary until it is forensically cleared.
3. Record actual RPO (based on `MAX(orders.created_at)` from Step 4) and RTO (incident declared → cutover verified at Step 6d) in the incident document.
4. Open a planned-maintenance ticket to delete the fence firewall rule (Step 1b) and clean up the DR instance after retention.
5. If RPO or RTO targets were exceeded, raise the next DR drill priority.

---

## Dry-run checklist (quarterly)

- [ ] Run `scripts/simulate_disaster_recovery.py` with the current operator-estimated durations; it should pass.
- [ ] In a non-production project, execute Steps 1–7 against a clone of production shape.
- [ ] Record measured RPO/RTO and update targets if they drift.
- [ ] File an issue tagged `dr-drill` with the run log attached.
