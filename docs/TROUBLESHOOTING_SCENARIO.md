 Documented Issue & Resolution

### Issue Encountered: GKE External Ingress Returning `502 Bad Gateway` and Unhealthy Network Endpoint Groups (NEGs) After Initial Deployment

#### 1. Symptom
After deploying `storefront-ui` and `order-service` to `meridian-gke-primary` and applying `k8s/04-ingress-and-mci.yaml`, the Global External Application Load Balancer provisioned a public IP address, but hitting `http://<GLOBAL_LB_IP>/` returned `HTTP 502 Server Error: The server encountered a temporary error and could not complete your request`. Meanwhile, `kubectl get pods -n meridian-prod` showed all 6 pods (`3/3` for `storefront-ui` and `3/3` for `order-service`) in `Running` and `Ready` state, and `kubectl port-forward svc/storefront-ui 8080:80 -n meridian-prod` worked locally.

#### 2. Diagnosis & Root-Cause Investigation
1. Checked the Ingress event stream and backend health status:
   ```bash
   kubectl describe ingress meridian-global-ingress -n meridian-prod
   gcloud compute backend-services get-health <BACKEND_SERVICE_NAME> --global
   ```
   Every pod endpoint inside the zonal Network Endpoint Group (`k8s1-...-storefront-ui-80-...`) reported `healthState: UNHEALTHY`.
2. Inspected the auto-generated Google Cloud Health Check associated with the NEG:
   - **Root Cause A (Readiness Probe / Health Check Path Mismatch)**: By default, if a Kubernetes `readinessProbe` is omitted or uses a TCP socket check, the GKE Ingress controller defaults the GCLB HTTP health check path to `/`. However, on `order-service`, `/` returned `HTTP 404 Not Found` (since its routes lived under `/api/orders` and `/readyz`), causing the Google Load Balancer health probe to mark all `order-service` pods unhealthy (`404 != 200`).
   - **Root Cause B (VPC Firewall Blocking GCLB Probers on Container Ports `8080`/`8081`)**: Because VPC-native clusters use container-native load balancing (`cloud.google.com/neg: '{"ingress": true}'`), health check probes from Google's edge ranges (`130.211.0.0/22` and `35.191.0.0/16`) connect directly to the Pod's `containerPort` (`8080` and `8081`) rather than NodePort `30000-32767`. Our custom VPC firewall initially only allowed ports `80` and `443` from `130.211.0.0/22`.

#### 3. Resolution Implemented
1. **Explicit HTTP Readiness Probes in Both Deployments (`k8s/02-storefront-ui.yaml` & `k8s/03-order-service.yaml`)**: Added explicit `readinessProbe.httpGet` blocks pointing to `/readyz` on port `8080` (`storefront-ui`) and port `8081` (`order-service`), returning `HTTP 200 {"ready": true}`. The GKE Ingress controller automatically inherits the pod's `readinessProbe` path and port when creating the GCLB health check.
2. **Updated VPC Firewall Rule (`terraform/vpc.tf` -> `google_compute_firewall.allow_gclb_health_checks`)**: Expanded the allowed TCP ports from `["80", "443"]` to `["80", "443", "8080", "8081", "10256"]` for source ranges `130.211.0.0/22` and `35.191.0.0/16` targeting tag `gke-meridian-node`.
3. **Verification**: Within 45 seconds of applying the firewall update and rolling the deployments, `gcloud compute backend-services get-health` transitioned all 6 NEG endpoints to `HEALTHY`, and external requests to the Global Load Balancer IP returned `HTTP 200` with sub-40ms latency.

*(Secondary Note: During observability setup, the Cloud Logging sink `meridian-gke-to-bigquery-sink` initially logged a permission error writing to BigQuery because the sink creates a unique Google-managed service account `service-<PROJECT_NUMBER>@gcp-sa-logging.iam.gserviceaccount.com`. We resolved this permanently in `terraform/observability_security.tf` by adding the `google_bigquery_dataset_iam_member.sink_bq_writer` resource granting `roles/bigquery.dataEditor` to `google_logging_project_sink.bigquery_log_sink.writer_identity`.)*

