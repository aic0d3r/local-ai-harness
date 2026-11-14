#!/usr/bin/env bash
# End-to-end install check, no GPU and no real engine: runs ./setup.sh against the stub engine
# (tests/windows-vm/stub-engine.py) in a throwaway HOME with a fake `pi`, then checks what it wrote and that
# `./harness verify` gets a live reply. Safe on a dev box: nothing outside the temp HOME is touched.
#   tests/e2e-setup.sh
# Needs :8080 free (the harness' Strata profile port) and, for the rtk download setup.sh does, network access.
# shellcheck disable=SC2034  # the check() expressions read RC, BEFORE, V1 ... through eval
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT=8080
fails=0
check() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }

if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then echo "port $PORT is already in use - stop that engine first" >&2; exit 2; fi

H=$(mktemp -d); trap '[ "${STUB:-0}" != 0 ] && kill "$STUB" 2>/dev/null; rm -rf "$H"' EXIT  # never `kill 0`: that signals the whole process group
mkdir -p "$H/bin"
printf '#!/bin/sh\nexit 0\n' > "$H/bin/pi"; chmod +x "$H/bin/pi"   # setup.sh only needs `pi` to exist and `pi install` to succeed
python3 "$REPO/tests/windows-vm/stub-engine.py" 127.0.0.1 $PORT >/dev/null 2>&1 & STUB=$!
for _ in $(seq 1 20); do (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null && break; sleep 0.25; done

# env -i: nothing of the caller's PI_HOME / PI_CODING_AGENT_DIR / XDG_* can point the install at a real config
run() { env -i HOME="$H" PATH="$H/bin:/usr/bin:/bin:/usr/local/bin" "$@"; }
A="$H/.pi/agent"

echo "== setup.sh --strata (pi-only install against the stub engine)"
OUT=$(run bash "$REPO/setup.sh" --strata "http://127.0.0.1:$PORT" --no-sidecar < /dev/null 2>&1); RC=$?
check "setup.sh exits 0" '[ "$RC" = 0 ]'
check "models.json has the strata provider and the stub model" \
  'python3 -c "import json,sys; m=json.load(open(sys.argv[1]))[\"providers\"][\"strata\"][\"models\"][0]; sys.exit(m[\"id\"]!=\"stub-model\" or m[\"contextWindow\"]!=65536)" "$A/models.json"'
check "extensions installed, NPU ones left out" '[ -f "$A/extensions/taint-gate.ts" ] && [ ! -e "$A/extensions/npu-retrieval.ts" ]'
check "mcp.json written" '[ -s "$A/mcp.json" ]'
check "sidecar opt-out recorded" '[ "$(cat "$A/harness-sidecar")" = 0 ]'
check "nothing written outside the temp HOME's .pi, .config, .local" '[ -z "$(find "$H" -mindepth 1 -maxdepth 1 ! -name bin ! -name .pi ! -name .config ! -name .local ! -name .cache)" ]'

echo "== second run changes nothing (idempotent)"
BEFORE=$(cd "$A" && find . -type f ! -name '*.log' -exec sha256sum {} + | sort)
run bash "$REPO/setup.sh" --strata "http://127.0.0.1:$PORT" --no-sidecar < /dev/null >/dev/null 2>&1; RC2=$?
AFTER=$(cd "$A" && find . -type f ! -name '*.log' -exec sha256sum {} + | sort)
check "second setup.sh exits 0" '[ "$RC2" = 0 ]'
check "second run leaves every file as it was" '[ "$BEFORE" = "$AFTER" ]'

echo "== PI_CODING_AGENT_DIR (pi's own override) is honoured by every writer, wizard and installer alike"
CUSTOM="$H/custom/agent"
rm -rf "$H/.pi"
env -i HOME="$H" PI_CODING_AGENT_DIR="$CUSTOM" PATH="$H/bin:/usr/bin:/bin:/usr/local/bin" \
  bash "$REPO/setup.sh" --strata "http://127.0.0.1:$PORT" --no-sidecar < /dev/null >/dev/null 2>&1; RC3=$?
check "setup.sh with a custom agent dir exits 0" '[ "$RC3" = 0 ]'
check "models.json, extensions, mcp.json and the sidecar answer all land in the custom dir" \
  '[ -s "$CUSTOM/models.json" ] && [ -f "$CUSTOM/extensions/taint-gate.ts" ] && [ -s "$CUSTOM/mcp.json" ] && [ "$(cat "$CUSTOM/harness-sidecar")" = 0 ] && [ -s "$CUSTOM/settings.json" ]'
check "nothing was written to ~/.pi/agent" '[ ! -e "$H/.pi/agent" ]'
rm -rf "$H/custom"

echo "== PI_HOME alone (the harness' sandbox variable) puts everything in \$PI_HOME/agent"
env -i HOME="$H" PI_HOME="$H/sandbox" PATH="$H/bin:/usr/bin:/bin:/usr/local/bin" \
  bash "$REPO/setup.sh" --strata "http://127.0.0.1:$PORT" --no-sidecar < /dev/null >/dev/null 2>&1; RC4=$?
check "setup.sh with PI_HOME exits 0" '[ "$RC4" = 0 ]'
check "files land in \$PI_HOME/agent and not in ~/.pi" '[ -s "$H/sandbox/agent/models.json" ] && [ -f "$H/sandbox/agent/extensions/taint-gate.ts" ] && [ ! -e "$H/.pi/agent" ]'

echo "== halogen-path install with no engine running (no checkpoint, nothing started)"
HAL="$H/halogen/agent"
env -i HOME="$H" PI_HOME="$H/halogen" PATH="$H/bin:/usr/bin:/bin:/usr/local/bin" \
  bash "$REPO/setup.sh" --no-sidecar < /dev/null >/dev/null 2>&1; RC6=$?
check "bare setup.sh exits 0" '[ "$RC6" = 0 ]'
check "halogen + llamacpp-tiny providers, defaults and the NPU extensions are in place" \
  'python3 -c "import json,sys; p=json.load(open(sys.argv[1]+\"/models.json\"))[\"providers\"]; s=json.load(open(sys.argv[1]+\"/settings.json\")); sys.exit(not (\"halogen\" in p and \"llamacpp-tiny\" in p and s[\"defaultProvider\"]==\"halogen\"))" "$HAL" && [ -f "$HAL/extensions/npu-retrieval.ts" ]'
check "no config temp files left behind" '[ -z "$(find "$H/halogen" -name "*.tmp")" ]'
echo "== a models.json that does not parse is left exactly as it was"
BAD="$H/bad/agent"; mkdir -p "$BAD"; printf '{ "providers": ' > "$BAD/models.json"
env -i HOME="$H" PI_HOME="$H/bad" PATH="$H/bin:/usr/bin:/bin:/usr/local/bin" \
  bash "$REPO/setup.sh" --no-sidecar < /dev/null >/dev/null 2>&1
check "the broken models.json is untouched" '[ "$(cat "$BAD/models.json")" = "{ \"providers\": " ]'
rm -rf "$H/halogen" "$H/bad"

echo "== uninstall, agent dir outside PI_HOME (PI_HOME set = sandbox: real systemd units are skipped)"
OUTSIDE="$H/elsewhere/agent"
env -i HOME="$H" PI_HOME="$H/sandbox" PI_CODING_AGENT_DIR="$OUTSIDE" PATH="$H/bin:/usr/bin:/bin:/usr/local/bin" \
  bash "$REPO/setup.sh" --strata "http://127.0.0.1:$PORT" --no-sidecar < /dev/null >/dev/null 2>&1
env -i HOME="$H" PI_HOME="$H/sandbox" PI_CODING_AGENT_DIR="$OUTSIDE" PATH="$H/bin:/usr/bin:/bin:/usr/local/bin" \
  bash "$REPO/scripts/uninstall.sh" --yes < /dev/null >/dev/null 2>&1; RC5=$?
BK=$(ls "$H"/pi-harness-uninstall-backup-*.tar.gz 2>/dev/null | head -1)
check "uninstall exits 0 and writes a backup" '[ "$RC5" = 0 ] && [ -s "$BK" ]'
check "the backup holds the custom agent dir's models.json" 'tar tzf "$BK" | grep -q "^agent/models.json$"'
check "the harness's extensions are gone from the custom dir" '[ ! -e "$OUTSIDE/extensions/taint-gate.ts" ]'
check "the provider it wrote is removed from models.json" '! grep -q strata "$OUTSIDE/models.json"'
rm -rf "$H/sandbox" "$H/elsewhere" "$H"/pi-harness-uninstall-backup-*.tar.gz

echo "== harness verify + doctor"
run python3 "$REPO/setup/harness.py" verify strata stub-model >/dev/null 2>&1; V1=$?
check "./harness verify gets a reply from the stub" '[ "$V1" = 0 ]'
kill "$STUB" 2>/dev/null; wait "$STUB" 2>/dev/null; STUB=0
run python3 "$REPO/setup/harness.py" verify strata stub-model >/dev/null 2>&1; V2=$?
check "./harness verify fails (exit 1) once the engine is gone" '[ "$V2" = 1 ]'

if [ "$fails" -gt 0 ]; then echo; echo "setup output:"; echo "$OUT" | tail -40; echo "$fails check(s) failed"; exit 1; fi
echo "all checks passed"
