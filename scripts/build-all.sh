#!/usr/bin/env bash
# Build images natively for the current host's architecture and push to
# REGISTRY. Designed to run on c7i (amd64) and c8g (arm64) separately,
# without QEMU cross-emulation.
#
# Tagging strategy:
#   - Each build pushes to ${IMAGE_TAG}-${arch}   e.g. v1-amd64 / v1-arm64
#   - After pushing, the script refreshes a multi-arch manifest at
#     ${IMAGE_TAG} pointing to whatever per-arch tags currently exist
#     in the registry. So the consumer-facing tag (v1) is always a
#     manifest list and run hosts keep pulling `:v1`.
#
# Override PLATFORM to cross-build (e.g. PLATFORM=linux/arm64 on x86 host),
# but that requires QEMU and is much slower - avoid in production.

set -euo pipefail

cd "$(dirname "$0")/.."

# Ensure docker + buildx + builder are ready (compose plugin too, harmless).
bash scripts/ensure-docker.sh

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

: "${REGISTRY:?set REGISTRY in .env (e.g. <account>.dkr.ecr.us-east-1.amazonaws.com/agentic-rl-bench)}"
: "${IMAGE_TAG:=v1}"

# Detect host arch -> docker platform string.
case "$(uname -m)" in
  x86_64|amd64)   HOST_ARCH=amd64 ;;
  aarch64|arm64)  HOST_ARCH=arm64 ;;
  *) echo "ERROR: unsupported host arch $(uname -m)" >&2; exit 1 ;;
esac
PLATFORM="${PLATFORM:-linux/${HOST_ARCH}}"
ARCH_SUFFIX="${PLATFORM#linux/}"

echo "==> native build for ${PLATFORM} (host: $(uname -m))"

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
  "b7-textgame:./workers/b7-textgame"
)

# ---------------------------------------------------------------
# ECR setup: login + ensure each per-image repository exists.
# ECR doesn't auto-create repos on push; missing repos return 401
# on blob HEAD which masquerades as an auth failure.
# ---------------------------------------------------------------
if [[ "${REGISTRY}" == *.dkr.ecr.*.amazonaws.com/* ]]; then
  if ! command -v aws >/dev/null 2>&1; then
    echo "ERROR: REGISTRY is ECR but aws CLI is not installed" >&2
    exit 1
  fi

  ECR_HOST="$(echo "${REGISTRY}" | cut -d/ -f1)"
  ECR_REGION="$(echo "${ECR_HOST}" | cut -d. -f4)"
  ECR_PREFIX="${REGISTRY#*/}"   # everything after first '/'

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

# ---------------------------------------------------------------
# Native single-arch build per image.
# --provenance=false keeps the manifest a plain image (no
# attestation manifest), which makes the later imagetools merge
# clean and unambiguous.
# ---------------------------------------------------------------
build() {
  local name="$1" ctx="$2"
  local arch_tag="${REGISTRY}/${name}:${IMAGE_TAG}-${ARCH_SUFFIX}"
  echo "==> building ${name} (${PLATFORM}) -> ${arch_tag}"
  docker buildx build \
    --platform "${PLATFORM}" \
    --provenance=false \
    -t "${arch_tag}" \
    --push \
    "${ctx}"
}

for entry in "${IMAGES[@]}"; do
  name="${entry%%:*}"
  ctx="${entry#*:}"
  build "${name}" "${ctx}"
done

# ---------------------------------------------------------------
# Refresh multi-arch manifest at ${IMAGE_TAG} pointing to whatever
# per-arch tags currently exist in the registry. After the *first*
# host (e.g. amd64) builds, ${IMAGE_TAG} will point to amd64 only;
# after the second host (arm64) builds, ${IMAGE_TAG} will be a
# proper fat manifest listing both.
# ---------------------------------------------------------------
echo "==> updating multi-arch manifests at :${IMAGE_TAG}"
for entry in "${IMAGES[@]}"; do
  name="${entry%%:*}"
  merged="${REGISTRY}/${name}:${IMAGE_TAG}"
  sources=()
  for suffix in amd64 arm64; do
    src="${REGISTRY}/${name}:${IMAGE_TAG}-${suffix}"
    if docker buildx imagetools inspect "${src}" >/dev/null 2>&1; then
      sources+=("${src}")
    fi
  done
  if (( ${#sources[@]} == 0 )); then
    echo "    [skip]    ${name} (no arch-tagged images found)"
    continue
  fi
  echo "    [merge]   ${merged} <- ${sources[*]}"
  docker buildx imagetools create -t "${merged}" "${sources[@]}" >/dev/null
done

echo "==> done."
echo "    arch-specific tag: ${REGISTRY}/<image>:${IMAGE_TAG}-${ARCH_SUFFIX}"
echo "    fat tag (consumer side): ${REGISTRY}/<image>:${IMAGE_TAG}"
