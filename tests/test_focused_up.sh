#!/usr/bin/env bash
set -Eeuo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
NEOVIDS_LIB_ONLY=1 source "$TEST_DIR/../scripts/neovids"

STARTUP_FOCUS=gamedev
volumes="$(required_volumes)"
[[ "$volumes" == $'ideas-hub-db-data\norchestrator-db-data' ]]
STARTUP_FOCUS=pov
[[ "$(required_volumes)" == *media-indexer_media-indexer-db-data* ]]
[[ "$(required_volumes)" == *video-composer_video-composer-data* ]]

calls="$(mktemp)"
trap 'rm -f "$calls"' EXIT
record() { printf '%s\n' "$*" >> "$calls"; }
require_docker() { :; }
require_command() { :; }
validate_media_indexer_hdd_host_path() { record media-bind-check; }
prepare_installation() { INSTALLATION_MODE=existing; }
ensure_network() { :; }
start_model_gateway() { record model-gateway; }
compose() { record "compose $*"; }
container_id() { printf 'fake-container'; }
wait_container_health() { record "container $1"; }
wait_http() { record "http $1 $2"; }
verify_database_contents() { :; }
build_missing_images() { record "build $STARTUP_FOCUS"; }
detect_pending_migrations() { PENDING_MIGRATIONS=(); record "migrations $STARTUP_FOCUS"; }
run_pending_migrations() { :; }
validate_qwen_topology() { :; }
health_stack() { record "health $1"; }
status_stack() { record "status ${1:-full}"; }

up_stack --focus gamedev
grep -q 'http Orchestrator readiness .*focus=gamedev' "$calls"
grep -q 'compose ideas-hub up --detach --no-build backend frontend' "$calls"
grep -q 'compose orchestrator up --detach --no-build api worker control-worker frontend' "$calls"
if grep -Eq 'media-indexer|video-composer|generation-worker|thumbnail-director|neoclips|model-gateway|media-bind-check' "$calls"; then
  printf 'GameDev started an unrelated service\n' >&2
  exit 1
fi

: > "$calls"
up_stack --focus pov
grep -q 'compose media-indexer up --detach --no-build api' "$calls"
grep -q 'compose video-composer up --detach --no-build video-composer' "$calls"
grep -q 'http Orchestrator readiness .*focus=pov' "$calls"
if grep -Eq 'generation-worker|thumbnail-director|neoclips|model-gateway' "$calls"; then
  printf 'POV started an unrelated service\n' >&2
  exit 1
fi

: > "$calls"
up_stack
grep -q 'compose ideas-hub up --detach --no-build backend frontend generation-worker' "$calls"
grep -q 'model-gateway' "$calls"
grep -q 'compose thumbnail-director up' "$calls"
grep -q 'compose neoclips up' "$calls"
grep -q 'health full' "$calls"

printf 'test_focused_up: PASS\n'
