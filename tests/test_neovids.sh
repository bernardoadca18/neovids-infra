#!/usr/bin/env bash
set -Eeuo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
NEOVIDS_LIB_ONLY=1 source "$TEST_DIR/../scripts/neovids"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

assert_mode() {
  local expected="$1" present="$2"
  FAKE_PRESENT="$present"
  detect_installation_mode
  [[ "$INSTALLATION_MODE" == "$expected" ]] || \
    fail "expected installation mode $expected, got $INSTALLATION_MODE"
}

required_volumes() {
  printf '%s\n' volume-a volume-b volume-c
}

docker() {
  [[ "$1" == "volume" && "$2" == "inspect" ]] || fail "unexpected fake docker call"
  case ",${FAKE_PRESENT}," in
    *",$3,"*) return 0 ;;
    *) return 1 ;;
  esac
}

assert_mode fresh ""
assert_mode existing "volume-a,volume-b,volume-c"
assert_mode partial "volume-b"

migration_is_current() {
  [[ "$1" != "media-indexer" ]]
}
detect_pending_migrations >/dev/null
[[ "${PENDING_MIGRATIONS[*]}" == "media-indexer:api" ]] || \
  fail "pending migration plan did not isolate media-indexer"

migration_is_current() {
  return 0
}
detect_pending_migrations >/dev/null
((${#PENDING_MIGRATIONS[@]} == 0)) || fail "current databases produced a migration plan"

if main up --invalid-option >/dev/null 2>&1; then
  fail "invalid up option was accepted"
else
  [[ "$?" == "2" ]] || fail "invalid up option did not return exit code 2"
fi

grep -Eq 'compose .*down.*--remove-orphans' "$TEST_DIR/../scripts/neovids" || \
  fail "safe project-scoped down command missing"
grep -q 'resources)' "$TEST_DIR/../scripts/neovids" || \
  fail "read-only resources command missing"
grep -q 'validate_qwen_topology' "$TEST_DIR/../scripts/neovids" || \
  fail "Qwen integrated topology validation missing"
grep -q 'alias qwen-voice is absent' "$TEST_DIR/../scripts/neovids" || \
  fail "Qwen alias validation missing"
if grep -Eq 'docker (system|volume|image|builder|network) prune|docker volume rm|down .*--volumes|down .*(-v)( |$)' \
  "$TEST_DIR/../scripts/neovids"; then
  fail "destructive Docker command found"
fi

printf 'test_neovids: PASS\n'
