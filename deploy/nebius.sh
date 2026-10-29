#!/usr/bin/env bash
# Deploy Ladder as a CPU-only Nebius Serverless Endpoint. All GPU work happens in Token Factory.
# Prereqs: nebius CLI installed and logged in (`nebius profile create`), docker, jq.
# Flags follow the Nebius AI endpoints docs; run `nebius ai endpoint create --help` if your CLI version differs.
set -euo pipefail

: "${NEBIUS_API_KEY:?export NEBIUS_API_KEY (Token Factory key) first}"
REGION="${REGION:-eu-north1}"
NAME="${NAME:-ladder}"
REGISTRY_NAME="${REGISTRY_NAME:-ladder}"

REGISTRY_ID=$(nebius registry list --format json | jq -r --arg n "$REGISTRY_NAME" '.items[]? | select(.metadata.name==$n) | .metadata.id' | head -1)
if [ -z "$REGISTRY_ID" ]; then
  REGISTRY_ID=$(nebius registry create --name "$REGISTRY_NAME" --format json | jq -r '.metadata.id')
fi
IMAGE="cr.${REGION}.nebius.cloud/${REGISTRY_ID#registry-}/${NAME}:latest"

nebius iam get-access-token | docker login "cr.${REGION}.nebius.cloud" --username iam --password-stdin
docker build --platform linux/amd64 -t "$IMAGE" "$(dirname "$0")/.."
docker push "$IMAGE"

nebius ai endpoint create \
  --name "$NAME" \
  --image "$IMAGE" \
  --platform cpu-e2 \
  --container-port 8000 \
  --env "NEBIUS_API_KEY=${NEBIUS_API_KEY}" \
  --env "LADDER_MAX_CONCURRENT=4" \
  --public

echo "Waiting for the endpoint…"
for _ in $(seq 1 60); do
  STATE=$(nebius ai endpoint get-by-name --name "$NAME" --format json | jq -r '.status.state // empty')
  echo "  state: ${STATE:-pending}"
  [ "$STATE" = "RUNNING" ] && break
  sleep 10
done
nebius ai endpoint get-by-name --name "$NAME" --format json | jq '.status'
