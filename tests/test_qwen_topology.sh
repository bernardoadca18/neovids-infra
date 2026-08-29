#!/usr/bin/env bash
set -Eeuo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INFRA_DIR="$(cd "$TEST_DIR/.." && pwd -P)"
WORKSPACE_DIR="$(cd "$INFRA_DIR/.." && pwd -P)"

config="$({
  cd "$WORKSPACE_DIR/qwen3-tts-workshop"
  NEOVIDS_NETWORK=neovids docker compose \
    --project-name qwen3-tts-workshop \
    --file docker-compose.yaml \
    --file "$INFRA_DIR/infra/orchestration/qwen3-tts-workshop.yml" \
    config --format json
})"

python3 -c '
import json
import sys

config = json.load(sys.stdin)
service_networks = config["services"]["qwen-tts"]["networks"]
assert "neovids" in service_networks, service_networks
assert "qwen-voice" in service_networks["neovids"].get("aliases", []), service_networks
network = config["networks"]["neovids"]
assert network.get("external") is True, network
assert network.get("name") == "neovids", network
' <<<"$config"

printf 'test_qwen_topology: PASS\n'
