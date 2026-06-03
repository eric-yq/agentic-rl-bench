#!/usr/bin/env bash
# Run the full benchmark suite on the current host (c7i or c8g).
#
# Workflow:
#   - ensure docker + compose v2
#   - login to ECR + pull all pinned images (no local rebuilds)
#   - bring up worker services
#   - launch orchestrator container which sweeps each benchmark
#   - tear everything down on completion
#
# Results land in ./results/ and (if S3_BUCKET set) under
# s3://${S3_BUCKET}/${S3_PREFIX}/<arch>/...

set -euo pipefail
cd "$(dirname "$0")/.."

# Make sure docker engine + compose v2 + buildx are present on the run host.
# Same script used at build time; idempotent if everything is already there.
bash scripts/ensure-docker.sh

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

: "${REGISTRY:?set REGISTRY in .env}"
: "${IMAGE_TAG:=v1}"

# shellcheck disable=SC1091
source scripts/_lib.sh

mkdir -p results

WORKERS=(
  b1-codeexec-worker
  b3-mock-api
  b4-webarena-static
  b4-playwright-worker
  b5-sql-runner
)

ecr_login_if_needed

# Pull all pre-built images so compose doesn't fall back to building locally.
compose_pull orchestrator "${WORKERS[@]}"

# B8 cold-start trials spawn this image directly via the docker socket.
docker pull python:3.11-slim || true

echo "==> bringing up worker services"
docker compose --profile all up -d --no-build "${WORKERS[@]}"

echo "==> waiting for services to settle"
sleep 10

echo "==> running orchestrator"
rc=0
docker compose --profile orchestrator run --rm --no-deps orchestrator || rc=$?

echo "==> tearing down"
docker compose --profile all down --remove-orphans

echo "==> done (rc=${rc})"
echo "Local results in ./results/"
[[ -n "${S3_BUCKET:-}" ]] && echo "S3 results in s3://${S3_BUCKET}/${S3_PREFIX:-agentic-rl-bench}/"
exit "${rc}"
