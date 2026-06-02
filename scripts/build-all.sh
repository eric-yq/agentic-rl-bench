#!/usr/bin/env bash
# Build all multi-arch images (linux/amd64 + linux/arm64) and push to REGISTRY.
# Run once on a build host with docker buildx; the same images then run
# unchanged on c7i and c8g target instances.
#
# Auto-installs Docker + buildx if missing (Linux only). On macOS, falls back
# to printing instructions and exits non-zero.

set -euo pipefail

cd "$(dirname "$0")/.."

# Ensure docker + buildx + binfmt + builder are ready.
# This handles install on bare AL2/AL2023/Ubuntu/RHEL hosts and
# is a no-op when everything is already present.
bash scripts/ensure-docker.sh

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

: "${REGISTRY:?set REGISTRY in .env (e.g. <account>.dkr.ecr.us-east-1.amazonaws.com/agentic-rl-bench)}"
: "${IMAGE_TAG:=v1}"

PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

# ECR login if REGISTRY looks like ECR and we're authenticated to AWS
if [[ "${REGISTRY}" == *.dkr.ecr.*.amazonaws.com/* ]]; then
  if command -v aws >/dev/null 2>&1; then
    ECR_HOST="$(echo "${REGISTRY}" | cut -d/ -f1)"
    ECR_REGION="$(echo "${ECR_HOST}" | cut -d. -f4)"
    echo "==> ECR login: ${ECR_HOST} (region ${ECR_REGION})"
    aws ecr get-login-password --region "${ECR_REGION}" \
      | docker login --username AWS --password-stdin "${ECR_HOST}" \
      || echo "(ECR login failed; continuing - push may fail)"
  else
    echo "(aws CLI not found; skipping ECR login)"
  fi
fi

build() {
  local name="$1" ctx="$2"
  echo "==> building ${name} from ${ctx}"
  docker buildx build \
    --platform "${PLATFORMS}" \
    -t "${REGISTRY}/${name}:${IMAGE_TAG}" \
    --push \
    "${ctx}"
}

build orchestrator         ./orchestrator
build b1-codeexec          ./workers/b1-codeexec
build b3-mock-api          ./workers/b3-mock-api
build b4-playwright        ./workers/b4-playwright
build b4-webarena-static   ./workers/b4-webarena-static
build b5-sql-runner        ./workers/b5-sql-runner

echo "All images pushed to ${REGISTRY} with tag ${IMAGE_TAG}"
