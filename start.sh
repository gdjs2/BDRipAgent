#!/usr/bin/env bash
# Start the complete application without installing Python or Node on the host.
set -euo pipefail

fail() { printf 'Startup failed: %s\n' "$*" >&2; exit 1; }

skip_login=false
gpu=false
update_encoder=false
for argument in "$@"; do
case "$argument" in
  --no-login) skip_login=true ;;
  --gpu) gpu=true ;;
  --update-encoder) update_encoder=true ;;
  -h|--help)
    printf 'Usage: ./start.sh [--gpu] [--no-login] [--update-encoder]\n\n'
    printf 'Prepare storage and update application services, preserving the encoder, then check Codex login.\n'
    printf 'Existing credentials and data are preserved. --no-login skips Codex authentication.\n'
    printf '%s\n' '--gpu gives the general worker NVIDIA GPU access for screenshot scanning.'
    printf '%s\n' '--update-encoder rebuilds the encoder after the queue is paused and active encoding tasks finish.'
    exit 0 ;;
  *) fail "Unknown option: $argument. Use --help for usage." ;;
esac
done

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_root"
for program in docker openssl flock; do
  command -v "$program" >/dev/null || fail "Install $program before running this command."
done
docker compose version >/dev/null 2>&1 || fail 'Install the Docker Compose plugin.'
docker info >/dev/null 2>&1 || fail 'Docker is unavailable. Start Docker and give your user access to it.'

mkdir -p .cache
exec 9>.cache/startup.lock
flock -n 9 || fail 'Another startup command is already running in this project.'

env_input=.env
[[ -f "$env_input" ]] || env_input=.env.example
env_temporary="$(mktemp "$project_root/.env.startup.XXXXXX")"
trap 'rm -f -- "$env_temporary"' EXIT
# Treat .env as data, never as executable shell code. Preserve custom values,
# comments and settings; only fill missing, empty or example secret values.
awk -v app_uid="${APP_UID:-${SUDO_UID:-$(id -u)}}" -v app_gid="${APP_GID:-${SUDO_GID:-$(id -g)}}" '
  function fresh(    command, value, result) {
    command = "openssl rand -hex 32"
    result = (command | getline value)
    close(command)
    if (result != 1 || length(value) != 64 || value !~ /^[0-9a-f]+$/) {
      print "Could not generate startup credentials" > "/dev/stderr"
      failed = 1
      exit 1
    }
    return value
  }
  BEGIN {
    keys[1] = "API_TOKEN"; keys[2] = "AGENT_TOKEN"; keys[3] = "POSTGRES_PASSWORD"
    for (i = 1; i <= 3; i++) wanted[keys[i]] = 1
  }
  {
    key = $0
    sub(/^[ \t]*(export[ \t]+)?/, "", key)
    sub(/[ \t]*=.*/, "", key)
    if ((key == "APP_UID" || key == "APP_GID") && $0 ~ /=/) {
      ids[key] = 1
      value = $0
      sub(/^[^=]*=[ \t]*/, "", value)
      sub(/[ \t]+#.*/, "", value)
      if (value == "" || value == "\"\"" || value == sprintf("%c%c", 39, 39)) {
        print key "=" (key == "APP_UID" ? app_uid : app_gid)
        next
      }
    }
    if (key in wanted && $0 ~ /=/) {
      if (seen[key]++) {
        print "Duplicate setting in .env: " key > "/dev/stderr"
        failed = 1
        exit 1
      }
      value = $0
      sub(/^[^=]*=[ \t]*/, "", value)
      sub(/[ \t]+#.*/, "", value)
      sub(/[ \t\r]+$/, "", value)
      quote = substr(value, 1, 1)
      if ((quote == "\"" || quote == sprintf("%c", 39)) && substr(value, length(value), 1) == quote)
        value = substr(value, 2, length(value) - 2)
      if (value == "" || value ~ /^replace-with-/) {
        print key "=" fresh()
        next
      }
    }
    print
  }
  END {
    if (!failed) {
      for (i = 1; i <= 3; i++) if (!seen[keys[i]]) print keys[i] "=" fresh()
      if (!("APP_UID" in ids)) print "APP_UID=" app_uid
      if (!("APP_GID" in ids)) print "APP_GID=" app_gid
    }
  }
' "$env_input" > "$env_temporary"
if [[ ! -f .env ]] || ! cmp -s .env "$env_temporary"; then
  mv -- "$env_temporary" .env
  printf 'Prepared .env with private credentials.\n'
fi

compose=(docker compose --project-directory "$project_root" --env-file "$project_root/.env" -f "$project_root/docker-compose.yml")
if "$gpu"; then
  compose+=(-f "$project_root/docker-compose.gpu.yml")
fi
# Let Compose handle quotes, variable interpolation and exported overrides itself.
# Capture this output: it contains credentials and must never be printed.
resolved_environment="$("${compose[@]}" config --environment)"
setting() {
  printf '%s\n' "$resolved_environment" | awk -v key="$1" 'index($0, key "=") == 1 {value = substr($0, length(key) + 2); found = 1} END {if (found) print value}'
}
api_token="$(setting API_TOKEN)"
agent_token="$(setting AGENT_TOKEN)"
database_password="$(setting POSTGRES_PASSWORD)"
[[ ${#api_token} -ge 24 ]] || fail 'API_TOKEN must have at least 24 characters; check .env and exported variables.'
[[ ${#agent_token} -ge 24 ]] || fail 'AGENT_TOKEN must have at least 24 characters; check .env and exported variables.'
[[ "$api_token" != "$agent_token" && "$api_token" != "$database_password" && "$agent_token" != "$database_password" ]] || fail 'Use different values for the three credentials.'
[[ "$database_password" =~ ^[a-zA-Z0-9._~-]+$ ]] || fail 'Use a URL-safe POSTGRES_PASSWORD (letters, numbers, dot, underscore, tilde or hyphen).'
app_uid="$(setting APP_UID)"
app_gid="$(setting APP_GID)"
[[ "$app_uid" =~ ^[0-9]+$ && "$app_gid" =~ ^[0-9]+$ && "$app_uid" -gt 0 && "$app_gid" -gt 0 ]] || fail 'Set APP_UID and APP_GID to your non-root host account (id -u and id -g).'
storage_root="$(setting STORAGE_ROOT)"
storage_root="${storage_root:-./data}"
mkdir -p -m 755 -- "$storage_root/incoming" "$storage_root/jobs" "$storage_root/completed" "$storage_root/artifacts" "$storage_root/cache/agent"

# Running encoders cannot move between containers. During the first upgrade,
# preserve a legacy mixed worker until its current encode/CRF tasks finish.
legacy_busy=false
if [[ -n "$("${compose[@]}" ps --status running -q worker)" ]]; then
  worker_state="$("${compose[@]}" exec -T worker python -c '
import socket
from sqlalchemy import select
from shared.config import get_settings
from shared.db import session
from shared.models import QueueSettings, Task
with session() as db:
    active = db.scalar(select(Task.id).where(Task.worker_id == socket.gethostname(), Task.status == "RUNNING", Task.type.in_(["encode", "crf_analysis"])))
    settings = db.get(QueueSettings, 1)
    ready = getattr(get_settings(), "worker_pool", "all") == "other" or (settings and settings.paused and not active)
    print("ready" if ready else "busy")
')" || fail 'Could not check the existing worker; left it running.'
  [[ "$worker_state" == ready ]] || legacy_busy=true
fi
encoder_exists="$("${compose[@]}" ps -a -q encoder)"
if "$update_encoder" && [[ -n "$encoder_exists" ]]; then
  encoder_host="$(docker inspect --format '{{.Config.Hostname}}' "$encoder_exists")" || fail 'Could not identify the encoder; left it unchanged.'
  encoder_state="$("${compose[@]}" exec -T api python -c '
import sys
from sqlalchemy import or_, select
from shared.db import session
from shared.models import QueueSettings, Task
with session() as db:
    settings = db.get(QueueSettings, 1)
    # Preserve CRF already running in an encoder from before the queue split.
    active = db.scalar(select(Task.id).where(Task.status == "RUNNING", or_(Task.type == "encode", Task.worker_id == sys.argv[1])))
    print("ready" if settings and settings.paused and not active else "busy")
' "$encoder_host")" || fail 'Could not check encoder readiness; left it running.'
  [[ "$encoder_state" == ready ]] || fail 'Pause the queue and wait for active encoder tasks to finish before --update-encoder. Normal ./start.sh preserves the encoder.'
fi

printf 'Building application services; existing encoder containers will be preserved.\n'
build_services=(api frontend worker crf agent permissions)
if "$legacy_busy"; then build_services+=(general-upgrade); fi
"${compose[@]}" build "${build_services[@]}"
printf 'Preparing generated-file ownership for %s:%s (source videos are untouched).\n' "$app_uid" "$app_gid"
"${compose[@]}" --profile maintenance run --rm --no-deps permissions
if ! "${compose[@]}" up -d --wait --wait-timeout 180 postgres redis; then
  fail 'Inspect startup errors with: docker compose logs --tail=100 postgres redis'
fi
if "$legacy_busy"; then
  # Stop accepting new deliveries, without signalling any active media process.
  # The bridge consumes the legacy queue and relays encoding jobs to the encoder.
  "${compose[@]}" exec -T worker python -c '
import socket
from worker.tasks import celery
node = "celery@" + socket.gethostname()
reply = celery.control.cancel_consumer("celery", destination=[node], reply=True, timeout=10)
if not any("ok" in item.get(node, {}) for item in reply):
    raise SystemExit("Could not drain the legacy consumer; existing encodes remain running")
'
fi
application_services=(api agent frontend crf)
if "$legacy_busy"; then application_services+=(general-upgrade); else application_services+=(worker); fi
if ! "${compose[@]}" up -d --no-deps --wait --wait-timeout 180 "${application_services[@]}"; then
  printf '\nInspect startup errors with: docker compose logs --tail=100 api worker encoder crf agent postgres\n' >&2
  exit 1
fi
if [[ -z "$encoder_exists" ]] || "$update_encoder"; then
  "${compose[@]}" build encoder
fi
encoder_options=(--no-recreate)
if "$update_encoder"; then encoder_options=(); fi
"${compose[@]}" up -d --no-deps --wait --wait-timeout 180 "${encoder_options[@]}" encoder
if "$legacy_busy"; then
  # A reboot must not revive the legacy consumer with its old pipeline code.
  # Its active processes remain untouched; future encodes use the new service.
  docker update --restart=no "$("${compose[@]}" ps -q worker)" >/dev/null
  printf 'Updated general worker is active. Legacy encodes continue uninterrupted. After they finish, pause the queue and run ./start.sh again to retire the bridge.\n'
elif [[ -n "$("${compose[@]}" ps --status running -q general-upgrade)" ]]; then
  "${compose[@]}" stop general-upgrade
fi
if "$update_encoder" && [[ -n "$encoder_exists" ]]; then
  printf 'Encoder updated. Resume the queue from the dashboard when ready.\n'
fi

# Retained exports from older versions may still use the old torrent root. The
# updated one-off worker relocates them before this empty directory is removed.
if [[ -d "$storage_root/torrents" ]]; then
  legacy_torrents="$(cd -- "$storage_root/torrents" && pwd)"
  printf 'Organizing legacy releases into ART directories.\n'
  "${compose[@]}" run --rm --no-deps -v "$legacy_torrents:/legacy-torrents" worker \
    python -m worker.pipeline.release_migration --legacy-torrents /legacy-torrents
  rmdir -- "$legacy_torrents" 2>/dev/null || true
fi

port="$(setting PORT)"
bind_address="$(setting BIND_ADDRESS)"
case "${bind_address:-127.0.0.1}" in
  0.0.0.0|127.0.0.1|::) browser_host=localhost ;;
  *) browser_host="$bind_address" ;;
esac
printf '\nDashboard: http://%s:%s\n' "$browser_host" "${port:-8080}"
printf 'Sign in using API_TOKEN from .env (or your exported API_TOKEN override).\n'
printf 'Incoming movies: %s/incoming\n' "$storage_root"
printf 'Release artifacts: %s/artifacts\n' "$storage_root"

if "$skip_login"; then
  printf '\nCodex login skipped. When ready: docker compose exec agent codex login --device-auth\n'
elif "${compose[@]}" exec -T agent codex login status >/dev/null 2>&1; then
  printf '\nCodex is already authenticated.\n'
elif [[ -t 0 && -t 1 ]]; then
  printf '\nSign in to Codex using the browser instructions below.\n'
  if ! "${compose[@]}" exec agent codex login --device-auth; then
    printf '\nThe server is running, but screenshot selection needs Codex login. Run ./start.sh again to retry.\n' >&2
    exit 1
  fi
else
  printf '\nThe server is running. Complete Codex login in a terminal:\n'
  printf '  docker compose exec agent codex login --device-auth\n'
fi
