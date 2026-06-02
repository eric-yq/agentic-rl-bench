#!/usr/bin/env bash
# Run the full benchmark suite on the current host (c7i or c8g).
#
# Workflow:
#   - bring up worker services (compose profile "all")
#   - launch orchestrator container which sweeps each benchmark
#   - on completion, tear everything down
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

mkdir -p results

# Pre-pull pinned image used by B8 cold-start tests
docker pull python:3.11-slim || true

echo "==> bringing up worker services"
docker compose --profile all up -d \
  b1-codeexec-worker b3-mock-api \
  b4-playwright-worker b4-webarena-static \
  b5-postgres

echo "==> waiting for services to be healthy"
sleep 10

echo "==> running orchestrator"
docker compose --profile orchestrator run --rm orchestrator || rc=$?
rc=${rc:-0}

echo "==> tearing down"
docker compose --profile all down --remove-orphans

echo "==> done (rc=${rc})"
echo "Local results in ./results/"
[[ -n "${S3_BUCKET:-}" ]] && echo "S3 results in s3://${S3_BUCKET}/${S3_PREFIX:-agentic-rl-bench}/"
exit "${rc}"
