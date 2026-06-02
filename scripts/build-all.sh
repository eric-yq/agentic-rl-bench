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

# Fetch HumanEval + MBPP-sanitized into orchestrator/datasets/ so they
# get baked into the orchestrator image. Skipped if already present.
bash scripts/fetch-datasets.sh

IMAGES=(
  "orchestrator:./orchestrator"
  "b1-codeexec:./workers/b1-codeexec"
  "b3-mock-api:./workers/b3-mock-api"
  "b4-playwright:./workers/b4-playwright"
  "b4-webarena-static:./workers/b4-webarena-static"
  "b5-sql-runner:./workers/b5-sql-runner"
)

# ECR setup: login + ensure each per-image repository exists.
# ECR doesn't auto-create repos on push; missing repos return HTTP 401
# on blob HEAD which looks like an auth failure but isn't.
if [[ "${REGISTRY}" == *.dkr.ecr.*.amazonaws.com/* ]]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "ERROR: REGISTRY is ECR but aws CLI is not installed" >&2
    exit 1
  fi

  ECR_HOST="$(echo "${REGISTRY}" | cut -d/ -f1)"
  ECR_REGION="$(echo "${ECR_HOST}" | cut -d. -f4)"
  # Repo prefix is everything after the first '/' in REGISTRY,
  # e.g. REGISTRY=<acct>.dkr.ecr.us-east-1.amazonaws.com/agentic-rl-bench
  #      ECR_PREFIX=agentic-rl-bench
  ECR_PREFIX="${REGISTRY#*/}"

  echo "==> ECR login: ${ECR_HOST} (region ${ECR_REGION})"
  aws ecr get-login-password --region "${ECR_REGION}" \
    | docker login --username AWS --password-stdin "${ECR_HOST}"

  echo "==> ensuring ECR repositories exist under ${ECR_PREFIX}/"
  for entry in "${IMAGES[@]}"; do
    name="${entry%%:*}"
    repo="${ECR_PREFIX}/${name}"
    if aws ecr describe-repositories \
         --region "${ECR_REGION}" \
         --repository-names "${repo}" >/dev/null 2>&1; then
      echo "    [ok]      ${repo}"
    else
      echo "    [create]  ${repo}"
      aws ecr create-repository \
        --region "${ECR_REGION}" \
        --repository-name "${repo}" \
        --image-scanning-configuration scanOnPush=true \
        --image-tag-mutability MUTABLE >/dev/null
    fi
  done
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

for entry in "${IMAGES[@]}"; do
  name="${entry%%:*}"
  ctx="${entry#*:}"
  build "${name}" "${ctx}"
done

echo "All images pushed to ${REGISTRY} with tag ${IMAGE_TAG}"
