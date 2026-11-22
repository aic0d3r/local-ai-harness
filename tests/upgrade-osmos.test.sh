#!/usr/bin/env bash
# scripts/upgrade-osmos.sh against a fake libosmos (a git repo with tags, a Makefile that "builds" a tiny osmos-api, and a
# bare remote), no GPU, no model, no network: the upgrade, "already current", your local edits carried over, a checkout
# that conflicts with them (refused before anything is stopped) and a release that fails to start (rolled back, old server
# serving again). Everything lives in a temp dir; it needs the port below free.
#   tests/upgrade-osmos.test.sh
# shellcheck disable=SC2034  # the check() expressions read OUT, RC ... through eval
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT=18091
fails=0
check() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }
if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then echo "port $PORT is already in use" >&2; exit 2; fi

T=$(mktemp -d); trap 'pkill -f "$T/" 2>/dev/null; rm -rf "$T"' EXIT
export HOME="$T/home" PI_CODING_AGENT_DIR="$T/agent"; mkdir -p "$HOME" "$PI_CODING_AGENT_DIR"
git config --global user.email t@t; git config --global user.name t; git config --global init.defaultBranch main
git config --global protocol.file.allow always

# the fake engine: GET /health, GET /v1/models, POST /v1/chat/completions -> "OK" and a usage count
cat > "$T/fake_api.py" <<'PY'
import http.server, json, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, o):
        b = json.dumps(o).encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self.send({"status": "ok"} if self.path == "/health" else {"data": [{"id": "osmos-test"}]})
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send({"choices": [{"message": {"content": "OK"}}], "usage": {"completion_tokens": 300}})
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
PY

# the "remote": v0.1.0 (the install), v0.2.0 (a normal release), v0.3.0 (its osmos-api dies at start), v0.4.0 (rewrites the line we edit)
mkdir "$T/src" && cd "$T/src" && git init -q
mkdir -p tools/install; printf '#!/bin/sh\n' > tools/install/osmos_install.py; chmod +x tools/install/osmos_install.py
cat > Makefile <<'MK'
all:
serve:
	@mkdir -p bin && printf '#!/bin/sh\n' > bin/osmos-serve && chmod +x bin/osmos-serve
api:
	@mkdir -p bin && { echo '#!/bin/sh'; echo '[ "$$1" = --version ] && { echo "osmos $(shell cat VERSION)"; exit 0; }'; \
	  echo '[ "$(shell cat BROKEN 2>/dev/null)" = 1 ] && exit 1'; echo 'exec python3 $(FAKE) "$$@"'; } > bin/osmos-api && chmod +x bin/osmos-api
MK
printf 'top\nmid\nedit-me\n' > host.txt
rel() { echo "$1" > VERSION; [ -n "${2:-}" ] && echo "$2" > BROKEN; [ -n "${3:-}" ] && echo "$3" > host.txt; git add -A; git commit -qm "$1"; git tag "v$1"; }
rel 0.1.0; sed -i 's/^top$/top-0.2/' host.txt; rel 0.2.0;  # 0.2.0 edits host.txt, away from our line
 git checkout -q -b broken; rel 0.3.0 1; git checkout -q main; git checkout -q -b conflict; rel 0.4.0 "" "release-edit"; git checkout -q main
git clone -q --bare "$T/src" "$T/origin.git"
git clone -q "$T/origin.git" "$T/libosmos"; cd "$T/libosmos" && git checkout -q v0.1.0 && git checkout -q -B main v0.1.0
mkdir -p "$T/models/osmos-evo"; echo '{}' > "$T/models/osmos-evo/osmos.json"
export OSMOS_DIR="$T/libosmos" OSMOS_MODELS="$T/models" OSMOS_PORT=$PORT OSMOS_POLL_S=1
export FAKE="$T/fake_api.py"
up() { (cd "$OSMOS_DIR" && make -s serve api >/dev/null) && setsid nohup bash "$REPO/server/start-osmos.sh" > "$T/start.log" 2>&1 < /dev/null & for _ in $(seq 1 30); do curl -sf -m1 "localhost:$PORT/health" >/dev/null && return 0; sleep 0.3; done; return 1; }
ver() { "$OSMOS_DIR/bin/osmos-api" --version; }
run() { bash "$REPO/scripts/upgrade-osmos.sh" "$@" 2>&1; }

echo "== v0.1.0 serving, upgrade to v0.2.0 with a local edit to a tracked file"
up; check "v0.1.0 is up" 'curl -sf localhost:$PORT/health >/dev/null'
echo "mine" >> "$OSMOS_DIR/host.txt"   # same file the release edits, other lines
OUT=$(run v0.2.0); RC=$?
check "exits 0 and says OK" '[ "$RC" = 0 ] && echo "$OUT" | grep -q "^OK: osmos"'
check "the binary is rebuilt at 0.2.0" '[ "$(ver)" = "osmos 0.2.0" ]'
check "osmos answers on the new build" 'curl -sf localhost:$PORT/health >/dev/null'
check "the local edit is still there, merged with the release's" 'grep -q "^mine$" "$OSMOS_DIR/host.txt" && grep -q "^top-0.2$" "$OSMOS_DIR/host.txt"'
check "the branch moved to the release (no detached HEAD)" '[ "$(git -C "$OSMOS_DIR" symbolic-ref --short -q HEAD)" = main ] && [ "$(git -C "$OSMOS_DIR" describe --tags)" = v0.2.0 ]'
check "the upgrade said it kept the local edit" 'echo "$OUT" | grep -q "keeping your local edits: host.txt"'

echo "== already on it"
OUT=$(run v0.2.0); check "second run: nothing to do, exit 0" '[ $? = 0 ] && echo "$OUT" | grep -q "nothing to do"'

echo "== a release that conflicts with the local edit is refused before anything is stopped"
git -C "$OSMOS_DIR" checkout -q -- host.txt 2>/dev/null; echo "mine-conflicting" > "$OSMOS_DIR/host.txt"
OUT=$(run v0.4.0); RC=$?
check "exit 1 with the reason" '[ "$RC" = 1 ] && echo "$OUT" | grep -q "v0.4.0 changes the same lines as your local edits"'
check "the old server was never stopped" 'curl -sf localhost:$PORT/health >/dev/null && [ "$(ver)" = "osmos 0.2.0" ]'
check "the edit and the checkout are as they were" '[ "$(cat "$OSMOS_DIR/host.txt")" = mine-conflicting ] && [ "$(git -C "$OSMOS_DIR" describe --tags)" = v0.2.0 ] && [ -z "$(git -C "$OSMOS_DIR" stash list)" ]'
git -C "$OSMOS_DIR" checkout -q -- host.txt

echo "== a release whose osmos-api dies at start is rolled back"
OUT=$(run v0.3.0); RC=$?
check "exit 1, says it rolled back" '[ "$RC" = 1 ] && echo "$OUT" | grep -q "rolling back to v0.2.0"'
check "0.2.0 is serving again before the script exits" 'echo "$OUT" | grep -q "v0.2.0 is serving again" && curl -sf localhost:$PORT/health >/dev/null'
check "the checkout and the binary are back on 0.2.0" '[ "$(git -C "$OSMOS_DIR" describe --tags)" = v0.2.0 ] && [ "$(ver)" = "osmos 0.2.0" ]'

echo "== a pack with a file gone is refused before anything is stopped"
P="$T/models/osmos-evo"; touch "$P/m-00001-of-00003.gguf" "$P/m-00002-of-00003.gguf"; ln -s "$T/nowhere" "$P/patch.gguf"
echo "{\"model\": \"$P/m-00001-of-00003.gguf\", \"overlay\": \"$P/patch.gguf\"}" > "$P/osmos.json"
OUT=$(bash "$REPO/server/start-osmos.sh" --check); RC=$?; WANT=$(printf '%s\n%s' "$P/patch.gguf" "$P/m-00003-of-00003.gguf")
check "--check lists the dangling overlay and the missing shard, not the present ones" '[ "$RC" = 1 ] && [ "$OUT" = "$WANT" ]'
OUT=$(run v0.3.0); RC=$?
check "exit 1 naming the file" '[ "$RC" = 1 ] && echo "$OUT" | grep -q "missing files" && echo "$OUT" | grep -q "patch.gguf"'
check "the old server was never stopped, nor the checkout moved" 'curl -sf localhost:$PORT/health >/dev/null && [ "$(git -C "$OSMOS_DIR" describe --tags)" = v0.2.0 ]'
touch "$P/m-00003-of-00003.gguf"; rm "$P/patch.gguf"; touch "$P/patch.gguf"
check "--check passes once every file is there" 'bash "$REPO/server/start-osmos.sh" --check'

echo; [ "$fails" = 0 ] && echo "all checks passed" || { echo "$fails check(s) failed"; echo "--- last output:"; echo "$OUT" | tail -15; exit 1; }
