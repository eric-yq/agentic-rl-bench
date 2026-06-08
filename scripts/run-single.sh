#!/usr/bin/env bash
# Run a single benchmark by ID (e.g. ./scripts/run-single.sh B3).
# Convenient for iterative debugging; brings up only the needed services.

set -euo pipefail
cd "$(dirname "$0")/.."

# Make sure docker engine + compose v2 plugin are present on the run host.
bash scripts/ensure-docker.sh

if [[ -f .env ]]; then
  # `set -a` exports anything we read from .env, but anything already
  # in the environment (passed on the command line or via `export`)
  # wins via this loop - we only set variables that aren't yet defined.
  while IFS='=' read -r key val; do
    # skip blanks and comments
    [[ -z "${key}" || "${key}" =~ ^[[:space:]]*# ]] && continue
    # strip leading 'export ' if present
    key="${key#export }"
    key="${key// /}"
    # only set if unset; cmdline / shell env overrides .env
    if [[ -z "${!key+x}" ]]; then
      # strip surrounding quotes from val if any
      val="${val%\"}"; val="${val#\"}"
      export "${key}=${val}"
    fi
  done < .env
fi

: "${REGISTRY:?set REGISTRY in .env}"
: "${IMAGE_TAG:=v1}"

# shellcheck disable=SC1091
source scripts/_lib.sh

BID="${1:?usage: run-single.sh <B1|B3|B4|B5|B7|B8|B9>}"
PROFILE="$(echo "${BID}" | tr '[:upper:]' '[:lower:]')"

# Map benchmark -> compose services it needs (besides the orchestrator).
case "${BID}" in
  B1) WORKERS=(b1-codeexec-worker) ;;
  B3) WORKERS=(b3-mock-api) ;;
  B4) WORKERS=(b4-webarena-static b4-playwright-worker) ;;
  B5) WORKERS=(b5-sql-runner) ;;
  B7) WORKERS=(b7-textgame) ;;
  B8) WORKERS=() ;;  # only needs docker socket; no long-running workers
  B9) WORKERS=(b1-codeexec-worker b3-mock-api b5-sql-runner) ;;
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
SKIP_LIST=$(printf "B1,B3,B4,B5,B7,B8,B9" | sed "s/${BID},*//;s/,${BID}//")
echo "==> running ${BID} (skipping: ${SKIP_LIST})"

rc=0
SKIP="${SKIP_LIST}" \
  docker compose --profile orchestrator run --rm --no-deps orchestrator || rc=$?

# Dump each worker's last 80 log lines before teardown - this is what
# you want when warmup failed because the worker didn't get healthy.
if (( ${#WORKERS[@]} > 0 )) && (( rc != 0 )); then
  for svc in "${WORKERS[@]}"; do
    echo "==> last logs from ${svc}:"
    docker compose --profile "${PROFILE}" logs --tail 80 "${svc}" || true
    echo
  done
fi

if (( ${#WORKERS[@]} > 0 )); then
  docker compose --profile "${PROFILE}" down --remove-orphans
fi

exit "${rc}"
