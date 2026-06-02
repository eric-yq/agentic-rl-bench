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
    echo "WARN: REGISTRY is ECR but aws CLI is not installed; pulls will fail" >&2
    return 0
  fi
  local host region
  host="$(echo "${REGISTRY}" | cut -d/ -f1)"
  region="$(echo "${host}" | cut -d. -f4)"
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
