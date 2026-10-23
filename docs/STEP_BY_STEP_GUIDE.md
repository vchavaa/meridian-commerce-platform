### Option A: Run Locally on Windows PC (PowerShell)
1. Unzip `meridian-commerce-platform.zip` and open **PowerShell** in the project folder:
   ```powershell
   cd meridian-commerce-platform
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install Flask matplotlib numpy
   .\run-local.ps1
   ```
2. To run the full 4-container multi-cluster topology in Docker Desktop on Windows:
   ```powershell
   docker compose up --build -d
   # Open http://localhost:8080 (Primary Cluster Storefront)
   # Open http://localhost:8082 (Secondary Cluster Storefront)
   ```

### Option B: Run Locally on Mac / Linux (Terminal)
1. Unzip `meridian-commerce-platform.zip` and run:
   ```bash
   cd meridian-commerce-platform
   python3 -m venv .venv
   source .venv/bin/activate
   pip install Flask matplotlib numpy
   ./run-local.sh
   ```
2. Or launch both simulated clusters via Docker Compose:
   ```bash
   docker compose up --build -d
   curl -i http://localhost:8080/healthz
   curl -i -X POST http://localhost:8080/api/checkout -H "Content-Type: application/json" -d '{"sku":"MRD-101","quantity":1}'
   ```

### Option C: Run in Google Colab / Jupyter Notebook
1. Upload `Meridian_Platform_Demo_Notebook.ipynb` to **Google Colab** (or open in VS Code / JupyterLab).
2. Click **Runtime -> Run all**. The notebook runs a local multi-cluster telemetry simulation, including four deterministic HTTP 5xx research cases with trace IDs, runs local SQL analyses, and displays the five-panel observability dashboard inline. It does not query BigQuery or retrieve live Google Cloud logs.

### Option D: Deploy to Google Cloud Platform (Terraform + GKE)
1. Authenticate and configure your project:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   export PROJECT_ID="your-gcp-project-id"
   gcloud config set project $PROJECT_ID
   ```
2. Provision the VPC, Subnets, Cloud NAT, Firewalls, 2 GKE Clusters, BigQuery Sink, and Cloud Armor via Terraform:
   ```bash
   cd terraform
   cp terraform.tfvars.example terraform.tfvars
   # Edit terraform.tfvars to set project_id = "your-gcp-project-id"
   terraform init
   terraform plan
   terraform apply -auto-approve
   ```
3. Build and push the container images to Artifact Registry:
   ```bash
   cd ..
   gcloud auth configure-docker us-central1-docker.pkg.dev
   docker build -t us-central1-docker.pkg.dev/$PROJECT_ID/meridian-containers/storefront-ui:1.0.0 ./apps/storefront-ui
   docker push us-central1-docker.pkg.dev/$PROJECT_ID/meridian-containers/storefront-ui:1.0.0

   docker build -t us-central1-docker.pkg.dev/$PROJECT_ID/meridian-containers/order-service:1.0.0 ./apps/order-service
   docker push us-central1-docker.pkg.dev/$PROJECT_ID/meridian-containers/order-service:1.0.0
   ```
4. Deploy the Kubernetes manifests to **both** Primary (`us-central1`) and Secondary (`us-east1`) clusters:
   ```bash
   # Replace PROJECT_ID placeholder in k8s manifests
   sed -i.bak "s/PROJECT_ID/$PROJECT_ID/g" k8s/*.yaml

   # Deploy to Cluster 1 (Primary)
   gcloud container clusters get-credentials meridian-gke-primary --zone us-central1-a --project $PROJECT_ID
   kubectl apply -f k8s/01-namespace-and-config.yaml
   kubectl apply -f k8s/02-storefront-ui.yaml
   kubectl apply -f k8s/03-order-service.yaml
   kubectl apply -f k8s/04-ingress-and-mci.yaml

   # Deploy to Cluster 2 (Secondary)
   gcloud container clusters get-credentials meridian-gke-secondary --zone us-east1-b --project $PROJECT_ID
   kubectl apply -f k8s/01-namespace-and-config.yaml
   kubectl apply -f k8s/02-storefront-ui.yaml
   kubectl apply -f k8s/03-order-service.yaml
   ```
5. Create BigQuery external tables `cloud_logs` and `live_docker_ingested_logs` in `cs-512516.logs_ingest` using valid GCS object URIs, JSONL format, and schema auto-detection. Grant the BigQuery service account access to the bucket.
6. Configure a Grafana BigQuery data source for project `cs-512516` and dataset `logs_ingest`, then import `observability/grafana-dashboard.json` (Dashboards -> New -> Import -> Upload JSON file). The dashboard queries both tables and includes error records with trace IDs.
