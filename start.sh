#!/usr/bin/env bash
# Start the complete application without installing Python or Node on the host.
set -euo pipefail

fail() { printf 'Startup failed: %s\n' "$*" >&2; exit 1; }

skip_login=false
gpu=false
for argument in "$@"; do
case "$argument" in
  --no-login) skip_login=true ;;
  --gpu) gpu=true ;;
  -h|--help)
    printf 'Usage: ./start.sh [--gpu] [--no-login]\n\n'
    printf 'Prepare configuration/storage, build and start all services, then check Codex login.\n'
    printf 'Existing credentials and data are preserved. --no-login skips Codex authentication.\n'
    printf '%s\n' '--gpu gives the worker NVIDIA GPU access for optional screenshot scanning.'
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
awk '
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
    if (!failed) for (i = 1; i <= 3; i++) if (!seen[keys[i]]) print keys[i] "=" fresh()
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
storage_root="$(setting STORAGE_ROOT)"
storage_root="${storage_root:-./data}"
mkdir -p -m 755 -- "$storage_root/incoming" "$storage_root/jobs" "$storage_root/completed" "$storage_root/cache/agent"

printf 'Building and starting BDRip Agent. The first build may take several minutes.\n'
if ! "${compose[@]}" up -d --build --wait --wait-timeout 180; then
  printf '\nInspect startup errors with: docker compose logs --tail=100 api worker agent postgres\n' >&2
  exit 1
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
