#!/usr/bin/env bash
# Render manifests from PROJECT_ID_PLACEHOLDER templates to actual values.
# Usage: ./scripts/apply_project_id.sh <PROJECT_ID> [<REGION>]
#
# Writes rendered files to ./rendered/ and does NOT modify the committed manifests.

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <PROJECT_ID> [<REGION>]"
  echo "Example: $0 my-gcp-project us-central1"
  exit 1
fi

PROJECT_ID="$1"
REGION="${2:-us-central1}"
OUT_DIR="${OUT_DIR:-rendered}"

if [ -z "$PROJECT_ID" ]; then
  echo "PROJECT_ID must be non-empty" >&2
  exit 1
fi

mkdir -p "$OUT_DIR/k8s"

for src in k8s/*.yaml; do
  dest="$OUT_DIR/${src}"
  sed -e "s/PROJECT_ID_PLACEHOLDER/${PROJECT_ID}/g" \
      -e "s/PROJECT_REGION_PLACEHOLDER/${REGION}/g" \
      "$src" > "$dest"
done

echo "Rendered manifests to $OUT_DIR/k8s/"
echo "Apply with: kubectl apply -f $OUT_DIR/k8s/"
