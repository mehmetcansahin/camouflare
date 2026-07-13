#!/usr/bin/env bash
set -Eeuo pipefail

image="${1:-camouflare:ci}"
container_name="camouflare-smoke-${GITHUB_RUN_ID:-local}-${RANDOM}"
api_token="camouflare-ci-smoke-token"
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
  --env CAMOUFOX_GEOIP=false \
  "${image}" >/dev/null

ready=0
for _ in $(seq 1 120); do
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
  echo "Camouflare did not become healthy in 120 seconds." >&2
  exit 1
fi

curl --silent --show-error --fail --max-time 90 \
  --header "Authorization: Bearer ${api_token}" \
  http://127.0.0.1:18191/ready >"${work_dir}/ready.json"

curl --silent --show-error --fail --max-time 90 \
  --header "Authorization: Bearer ${api_token}" \
  --header 'Content-Type: application/json' \
  --data '{"cmd":"request.get","url":"http://host.docker.internal:18080/","maxTimeout":60000}' \
  http://127.0.0.1:18191/v1 >"${work_dir}/solution.json"

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
