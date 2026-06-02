#!/usr/bin/env bash
# Shared helpers for run-suite.sh / run-single.sh.
# Intended to be sourced, not executed.

# ECR login if REGISTRY points at ECR. Required so `docker compose pull`
# can fetch images we built earlier; otherwise compose falls back to a
# local rebuild from the `build:` section.
ecr_login_if_needed() {
  if [[ "${REGISTRY:-}" != *.dkr.ecr.*.amazonaws.com/* ]]; then
    return 0
  fi
  if ! command -v aws >/dev/null 2>&1; then
    echo "ERROR: REGISTRY is ECR but aws CLI is not installed" >&2
    return 1
  fi
  local host region
  host="$(echo "${REGISTRY}" | cut -d/ -f1)"
  region="$(echo "${host}" | cut -d. -f4)"

  # Verify AWS credentials before trying ECR. Otherwise the failure path
  # is the very confusing "Cannot perform an interactive login from a
  # non TTY device" (docker login falling back to a prompt because
  # `aws ecr get-login-password` printed nothing).
  if ! aws sts get-caller-identity --region "${region}" >/dev/null 2>&1; then
    cat >&2 <<EOF
ERROR: AWS credentials are not configured on this host.
       \`aws sts get-caller-identity\` failed.

Fix one of:
  - attach an IAM instance profile with AmazonEC2ContainerRegistryReadOnly
    (or stronger) and rerun  - takes effect immediately, no reboot needed
  - export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (+ AWS_SESSION_TOKEN)
  - run \`aws configure\` to write ~/.aws/credentials

REGISTRY=${REGISTRY}
EOF
    return 1
  fi

  echo "==> ECR login: ${host} (region ${region})"
  aws ecr get-login-password --region "${region}" \
    | docker login --username AWS --password-stdin "${host}"
}

# Pull a list of compose services without building. Fails loudly if a
# pull fails - we'd rather see the auth error than silently rebuild.
compose_pull() {
  echo "==> pulling images: $*"
  docker compose pull --quiet "$@"
}
