#!/usr/bin/env bash
# Run a single benchmark by ID (e.g. ./scripts/run-single.sh B3).
# Convenient for iterative debugging; brings up only the needed services.

set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

BID="${1:?usage: run-single.sh <B1|B3|B4|B5|B8>}"
PROFILE="$(echo "${BID}" | tr '[:upper:]' '[:lower:]')"

# B8 doesn't need worker services; orchestrator hits docker socket directly.
if [[ "${BID}" != "B8" ]]; then
  docker compose --profile "${PROFILE}" up -d
  sleep 8
fi

mkdir -p results
SKIP_LIST=$(printf "B1,B3,B4,B5,B8" | sed "s/${BID},*//;s/,${BID}//")
echo "==> running ${BID} (skipping: ${SKIP_LIST})"

SKIP="${SKIP_LIST}" \
  docker compose --profile orchestrator run --rm orchestrator || rc=$?
rc=${rc:-0}

[[ "${BID}" != "B8" ]] && docker compose --profile "${PROFILE}" down

exit "${rc}"
