#!/usr/bin/env bash
set -Eeuo pipefail

image="${1:-camouflare:ci}"
container_name="camouflare-smoke-${GITHUB_RUN_ID:-local}-${RANDOM}"
api_token="camouflare-ci-smoke-token"
pool_acquire_timeout_ms="${CAMOUFLARE_SMOKE_POOL_ACQUIRE_TIMEOUT_MS:-30000}"
readiness_timeout_ms="${CAMOUFLARE_SMOKE_READINESS_TIMEOUT_MS:-15000}"
request_timeout_ms="${CAMOUFLARE_SMOKE_REQUEST_TIMEOUT_MS:-60000}"
curl_timeout_seconds="${CAMOUFLARE_SMOKE_CURL_TIMEOUT_SECONDS:-90}"
startup_timeout_seconds="${CAMOUFLARE_SMOKE_STARTUP_TIMEOUT_SECONDS:-120}"

if [[ ! "${pool_acquire_timeout_ms}" =~ ^[1-9][0-9]*$ ]]; then
  echo "CAMOUFLARE_SMOKE_POOL_ACQUIRE_TIMEOUT_MS must be a positive integer." >&2
  exit 2
fi
if [[ ! "${readiness_timeout_ms}" =~ ^[1-9][0-9]*$ ]]; then
  echo "CAMOUFLARE_SMOKE_READINESS_TIMEOUT_MS must be a positive integer." >&2
  exit 2
fi
if [[ ! "${request_timeout_ms}" =~ ^[1-9][0-9]*$ ]]; then
  echo "CAMOUFLARE_SMOKE_REQUEST_TIMEOUT_MS must be a positive integer." >&2
  exit 2
fi
if [[ ! "${curl_timeout_seconds}" =~ ^[1-9][0-9]*$ ]]; then
  echo "CAMOUFLARE_SMOKE_CURL_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 2
fi
if [[ ! "${startup_timeout_seconds}" =~ ^[1-9][0-9]*$ ]]; then
  echo "CAMOUFLARE_SMOKE_STARTUP_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 2
fi
if ((curl_timeout_seconds * 1000 <= readiness_timeout_ms)); then
  echo "CAMOUFLARE_SMOKE_CURL_TIMEOUT_SECONDS must exceed the readiness timeout." >&2
  exit 2
fi
if ((curl_timeout_seconds * 1000 <= request_timeout_ms)); then
  echo "CAMOUFLARE_SMOKE_CURL_TIMEOUT_SECONDS must exceed the request timeout." >&2
  exit 2
fi

work_dir="$(mktemp -d)"

cleanup() {
  docker rm -f "${container_name}" >/dev/null 2>&1 || true
  if [[ -n "${fixture_pid:-}" ]]; then
    kill "${fixture_pid}" >/dev/null 2>&1 || true
    wait "${fixture_pid}" >/dev/null 2>&1 || true
  fi
  rm -rf "${work_dir}"
}
trap cleanup EXIT

printf '<!doctype html><title>Camouflare smoke</title><p id="fixture">local fixture</p>\n' \
  >"${work_dir}/index.html"
python3 -m http.server 18080 --bind 0.0.0.0 --directory "${work_dir}" \
  >"${work_dir}/fixture.log" 2>&1 &
fixture_pid=$!

docker run --detach --name "${container_name}" \
  --add-host host.docker.internal:host-gateway \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --shm-size 2g \
  --memory 4g \
  --pids-limit 512 \
  --publish 127.0.0.1:18191:8191 \
  --env HOST=0.0.0.0 \
  --env CAMOUFLARE_API_TOKEN="${api_token}" \
  --env HEADLESS=virtual \
  --env POOL_ACQUIRE_TIMEOUT_MS="${pool_acquire_timeout_ms}" \
  --env READINESS_TIMEOUT_MS="${readiness_timeout_ms}" \
  "${image}" >/dev/null

ready=0
for _ in $(seq 1 "${startup_timeout_seconds}"); do
  if curl --silent --fail --max-time 2 http://127.0.0.1:18191/health >/dev/null; then
    ready=1
    break
  fi
  if ! docker container inspect --format '{{.State.Running}}' "${container_name}" 2>/dev/null \
    | grep -qx true; then
    docker logs "${container_name}"
    exit 1
  fi
  sleep 1
done
if [[ "${ready}" != 1 ]]; then
  docker logs "${container_name}"
  echo "Camouflare did not become healthy in ${startup_timeout_seconds} seconds." >&2
  exit 1
fi

if ! curl --silent --show-error --fail-with-body --max-time "${curl_timeout_seconds}" \
  --header "Authorization: Bearer ${api_token}" \
  http://127.0.0.1:18191/ready >"${work_dir}/ready.json"; then
  sed -n '1,40p' "${work_dir}/ready.json" >&2
  docker logs "${container_name}"
  exit 1
fi

if ! curl --silent --show-error --fail-with-body --max-time "${curl_timeout_seconds}" \
  --header "Authorization: Bearer ${api_token}" \
  --header 'Content-Type: application/json' \
  --data "{\"cmd\":\"request.get\",\"url\":\"http://host.docker.internal:18080/\",\"maxTimeout\":${request_timeout_ms}}" \
  http://127.0.0.1:18191/v1 >"${work_dir}/solution.json"; then
  sed -n '1,40p' "${work_dir}/solution.json" >&2
  docker logs "${container_name}"
  exit 1
fi

python3 - "${work_dir}/solution.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("status") != "ok":
    raise SystemExit(f"container smoke returned an error: {payload}")
solution = payload.get("solution") or {}
if solution.get("status") != 200 or "local fixture" not in solution.get("response", ""):
    raise SystemExit(f"container smoke returned an unexpected solution: {payload}")
PY

echo "Container health, readiness, and local /v1 smoke passed."
