#!/usr/bin/env python3
"""
Meridian Commerce Platform - IAM & RBAC Policy Specification + Live Check.

The specification matrix (4 personas x 6 action classes = 24 policy entries)
is a declaration of intent.  The live mode shells out to `kubectl auth can-i`
against the active cluster context and *compares* the live cluster's
authorization decisions to the spec. Mismatches exit non-zero.

Usage:
  python scripts/verify_iam_roles.py                # print the specification + render matrix PNG
  python scripts/verify_iam_roles.py --live         # run kubectl auth can-i; exit 1 on mismatch
  python scripts/verify_iam_roles.py --live --namespace meridian-prod

Live mode requires:
  - a kubeconfig with cluster credentials for the target cluster
  - the ServiceAccounts in k8s/08-iam-rbac-roles.yaml applied to that cluster
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OBS_DIR = BASE_DIR / "observability"
OBS_DIR.mkdir(parents=True, exist_ok=True)

IAM_ROLES_POLICY = {
    "dev": {
        "display_name": "Developer (Dev)",
        "gcp_roles": ["roles/container.viewer", "roles/logging.viewer", "roles/cloudtrace.user"],
        "k8s_sa": "system:serviceaccount:meridian-prod:meridian-dev-sa",
        "permissions": {
            "view_pods_and_logs": True,
            "view_traces_and_metrics": True,
            "deploy_container_images": False,
            "toggle_region_drain": False,
            "scale_hpa_and_ingress": False,
            "read_production_secrets": False,
        },
    },
    "ops": {
        "display_name": "Operations (Ops)",
        "gcp_roles": ["roles/container.clusterViewer", "roles/monitoring.editor", "roles/compute.networkAdmin"],
        "k8s_sa": "system:serviceaccount:meridian-prod:meridian-ops-sa",
        "permissions": {
            "view_pods_and_logs": True,
            "view_traces_and_metrics": True,
            "deploy_container_images": False,
            "toggle_region_drain": True,
            "scale_hpa_and_ingress": True,
            "read_production_secrets": False,
        },
    },
    "sre": {
        "display_name": "Site Reliability Eng (SRE)",
        "gcp_roles": ["roles/container.admin", "roles/compute.loadBalancerAdmin", "roles/secretmanager.secretAccessor"],
        "k8s_sa": "system:serviceaccount:meridian-prod:meridian-sre-sa",
        "permissions": {
            "view_pods_and_logs": True,
            "view_traces_and_metrics": True,
            "deploy_container_images": True,
            "toggle_region_drain": True,
            "scale_hpa_and_ingress": True,
            "read_production_secrets": True,
        },
    },
    "cicd": {
        "display_name": "CI/CD Pipeline (CI/CD)",
        "gcp_roles": ["roles/artifactregistry.writer", "roles/container.developer"],
        "k8s_sa": "system:serviceaccount:meridian-prod:meridian-cicd-sa",
        "permissions": {
            "view_pods_and_logs": True,
            "view_traces_and_metrics": False,
            "deploy_container_images": True,
            "toggle_region_drain": False,
            "scale_hpa_and_ingress": False,
            "read_production_secrets": False,
        },
    },
}

ACTIONS = [
    ("view_pods_and_logs", "View GKE Pods & Container Logs (get pods/log)"),
    ("view_traces_and_metrics", "Query BigQuery Logs & Grafana Metrics"),
    ("deploy_container_images", "Push Images & Update Deployments (CI/CD Rollout)"),
    ("toggle_region_drain", "Drain Region / Traffic Flip (DRAIN_REGION / capacity-scaler)"),
    ("scale_hpa_and_ingress", "Modify HPA Replicas & Ingress Routing Rules"),
    ("read_production_secrets", "Access Production Secrets (Secret Manager / K8s Secret)"),
]

# Live-mode mapping: each permission -> (verb, resource) pair in Kubernetes.
# "deploy_container_images" is a cloud action, not a K8s one; we approximate
# it as 'update deployments' which is what the K8s side of a CI/CD rollout does.
LIVE_PROBES = {
    "view_pods_and_logs": [("get", "pods"), ("get", "pods/log")],
    "view_traces_and_metrics": [],  # external; not probable by kubectl - treat as skipped
    "deploy_container_images": [("update", "deployments.apps")],
    "toggle_region_drain": [("patch", "configmaps")],
    "scale_hpa_and_ingress": [("patch", "horizontalpodautoscalers.autoscaling")],
    "read_production_secrets": [("get", "secrets")],
}


def run_spec_mode():
    print("=" * 92)
    print(" MERIDIAN - IAM & RBAC POLICY SPECIFICATION (4 personas x 6 action classes = 24 entries)")
    print("=" * 92)
    print(" This matrix declares intended policy. For a live cluster check, run with --live.")
    print()

    for role_key, spec in IAM_ROLES_POLICY.items():
        print(f"[Persona: {spec['display_name']}]")
        print(f"  GCP IAM Equivalent : {', '.join(spec['gcp_roles'])}")
        print(f"  Local K8s Identity : {spec['k8s_sa']}")
        for act_key, act_label in ACTIONS:
            allowed = spec["permissions"][act_key]
            verdict = "ALLOW (expected)" if allowed else "DENY  (expected)"
            print(f"    -> {act_label:58s} : {verdict}")
        print()

    try:
        _render_matrix_png()
    except ImportError:
        print("[note] matplotlib not installed; skipping PNG matrix render.", file=sys.stderr)


def _render_matrix_png():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches

    fig, ax = plt.subplots(figsize=(14, 6.8), dpi=180, facecolor="#0f172a")
    ax.set_facecolor("#0f172a")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 52)
    ax.axis("off")

    ax.text(
        50, 48.5,
        "Meridian - IAM & Kubernetes RBAC Policy Specification Matrix",
        ha="center", va="center", fontsize=13.5, fontweight="bold", color="#f8fafc",
    )
    ax.text(
        50, 45.5,
        "Declared least-privilege separation across Dev, Ops, SRE, and CI/CD. Run with --live to verify against a cluster.",
        ha="center", va="center", fontsize=9.2, color="#94a3b8",
    )

    col_x = [3, 44, 58, 72, 86]
    col_w = [40, 13, 13, 13, 11]
    headers = ["Platform Action / Resource Scope", "Dev", "Ops", "SRE", "CI/CD"]
    for cx, cw, h_text in zip(col_x, col_w, headers):
        rect = patches.FancyBboxPatch(
            (cx, 39.5), cw - 0.6, 4.2,
            boxstyle="round,pad=0.1,rounding_size=0.4",
            facecolor="#1e3a8a", edgecolor="#3b82f6", lw=1.2,
        )
        ax.add_patch(rect)
        ax.text(cx + (cw - 0.6) / 2, 41.6, h_text, ha="center", va="center", fontsize=9.5, fontweight="bold", color="#ffffff")

    role_order = ["dev", "ops", "sre", "cicd"]
    for idx, (act_key, act_label) in enumerate(ACTIONS):
        y = 33.5 - idx * 5.2
        row_bg = "#1e293b" if idx % 2 == 0 else "#172033"
        rect = patches.FancyBboxPatch(
            (col_x[0], y), col_w[0] - 0.6, 4.4,
            boxstyle="round,pad=0.1,rounding_size=0.4",
            facecolor=row_bg, edgecolor="#334155", lw=1.0,
        )
        ax.add_patch(rect)
        ax.text(col_x[0] + 1.2, y + 2.2, act_label, ha="left", va="center", fontsize=8.8, color="#e2e8f0", fontweight="bold")

        for r_i, r_key in enumerate(role_order, start=1):
            allowed = IAM_ROLES_POLICY[r_key]["permissions"][act_key]
            bg = "#14532d" if allowed else "#7f1d1d"
            border = "#22c55e" if allowed else "#ef4444"
            txt = "ALLOW" if allowed else "DENY"
            txt_col = "#86efac" if allowed else "#fca5a5"
            c_rect = patches.FancyBboxPatch(
                (col_x[r_i], y), col_w[r_i] - 0.6, 4.4,
                boxstyle="round,pad=0.1,rounding_size=0.4",
                facecolor=bg, edgecolor=border, lw=1.1,
            )
            ax.add_patch(c_rect)
            ax.text(col_x[r_i] + (col_w[r_i] - 0.6) / 2, y + 2.2, txt, ha="center", va="center", fontsize=8.5, fontweight="bold", color=txt_col)

    ax.text(
        50, 3.2,
        "Spec check:  python scripts/verify_iam_roles.py          Live check:  python scripts/verify_iam_roles.py --live",
        ha="center", va="center", fontsize=8.5, color="#38bdf8", family="monospace",
    )

    out_png = OBS_DIR / "iam_rbac_audit_matrix.png"
    fig.savefig(out_png, dpi=180, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"\nSaved specification matrix PNG to: {out_png.relative_to(BASE_DIR)}")


def run_live_mode(namespace: str) -> int:
    """Shell out to `kubectl auth can-i` for each probe and compare to the spec."""
    results = []
    mismatches = 0
    skipped = 0

    for role_key, spec in IAM_ROLES_POLICY.items():
        for act_key, _ in ACTIONS:
            expected = spec["permissions"][act_key]
            probes = LIVE_PROBES.get(act_key) or []
            if not probes:
                results.append({
                    "persona": role_key,
                    "action": act_key,
                    "expected": "ALLOW" if expected else "DENY",
                    "actual": "SKIPPED",
                    "match": None,
                    "note": "No kubectl probe; verify via GCP IAM / Grafana ACLs externally.",
                })
                skipped += 1
                continue

            per_probe_results = []
            all_allow = True
            all_deny = True
            for verb, resource in probes:
                cmd = [
                    "kubectl", "auth", "can-i", verb, resource,
                    "-n", namespace,
                    "--as", spec["k8s_sa"],
                ]
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                except FileNotFoundError:
                    print("kubectl not found on PATH; cannot run --live mode.", file=sys.stderr)
                    return 2
                except subprocess.TimeoutExpired:
                    per_probe_results.append({"verb": verb, "resource": resource, "actual": "TIMEOUT"})
                    all_allow = all_deny = False
                    continue

                verdict = result.stdout.strip().lower()
                if verdict.startswith("yes"):
                    per_probe_results.append({"verb": verb, "resource": resource, "actual": "ALLOW"})
                    all_deny = False
                elif verdict.startswith("no"):
                    per_probe_results.append({"verb": verb, "resource": resource, "actual": "DENY"})
                    all_allow = False
                else:
                    per_probe_results.append({"verb": verb, "resource": resource, "actual": f"UNKNOWN:{verdict!r}"})
                    all_allow = all_deny = False

            if all_allow:
                actual = "ALLOW"
            elif all_deny:
                actual = "DENY"
            else:
                actual = "MIXED"

            expected_label = "ALLOW" if expected else "DENY"
            match = actual == expected_label
            if not match:
                mismatches += 1
            results.append({
                "persona": role_key,
                "action": act_key,
                "expected": expected_label,
                "actual": actual,
                "match": match,
                "probes": per_probe_results,
            })

    total = len(results)
    probed = total - skipped
    passed = probed - mismatches

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "totals": {
            "entries": total,
            "probed": probed,
            "passed": passed,
            "mismatches": mismatches,
            "skipped_external": skipped,
        },
        "results": results,
    }
    print(json.dumps(report, indent=2))
    return 0 if mismatches == 0 else 1


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--live", action="store_true", help="Shell out to kubectl auth can-i and compare to the spec; exit 1 on mismatch")
    parser.add_argument("--namespace", default="meridian-prod", help="Kubernetes namespace to probe in --live mode")
    args = parser.parse_args()

    if args.live:
        sys.exit(run_live_mode(args.namespace))
    else:
        run_spec_mode()


if __name__ == "__main__":
    main()
