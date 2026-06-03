#!/usr/bin/env bash
# Run a single benchmark by ID (e.g. ./scripts/run-single.sh B3).
# Convenient for iterative debugging; brings up only the needed services.

set -euo pipefail
cd "$(dirname "$0")/.."

# Make sure docker engine + compose v2 plugin are present on the run host.
bash scripts/ensure-docker.sh

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

: "${REGISTRY:?set REGISTRY in .env}"
: "${IMAGE_TAG:=v1}"

# shellcheck disable=SC1091
source scripts/_lib.sh

BID="${1:?usage: run-single.sh <B1|B3|B4|B5|B8>}"
PROFILE="$(echo "${BID}" | tr '[:upper:]' '[:lower:]')"

# Map benchmark -> compose services it needs (besides the orchestrator).
case "${BID}" in
  B1) WORKERS=(b1-codeexec-worker) ;;
  B3) WORKERS=(b3-mock-api) ;;
  B4) WORKERS=(b4-webarena-static b4-playwright-worker) ;;
  B5) WORKERS=(b5-sql-runner) ;;
  B8) WORKERS=() ;;  # only needs docker socket; no long-running workers
  *)  echo "unknown benchmark id: ${BID}"; exit 2 ;;
esac

ecr_login_if_needed

# Pull pinned images so compose doesn't try to rebuild on cache miss.
PULL=("orchestrator" "${WORKERS[@]}")
compose_pull "${PULL[@]}"

# B8 cold-start trials need this image present locally.
if [[ "${BID}" == "B8" ]]; then
  docker pull python:3.11-slim || true
fi

if (( ${#WORKERS[@]} > 0 )); then
  echo "==> bringing up workers for ${BID}: ${WORKERS[*]}"
  docker compose --profile "${PROFILE}" up -d --no-build "${WORKERS[@]}"
  sleep 8
fi

mkdir -p results
SKIP_LIST=$(printf "B1,B3,B4,B5,B8" | sed "s/${BID},*//;s/,${BID}//")
echo "==> running ${BID} (skipping: ${SKIP_LIST})"

rc=0
SKIP="${SKIP_LIST}" \
  docker compose --profile orchestrator run --rm --no-deps orchestrator || rc=$?

if (( ${#WORKERS[@]} > 0 )); then
  docker compose --profile "${PROFILE}" down --remove-orphans
fi

exit "${rc}"
