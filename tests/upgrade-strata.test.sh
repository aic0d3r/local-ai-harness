#!/usr/bin/env bash
# scripts/upgrade-strata.sh (Docker route) against a fake docker: a script in PATH whose "images" and "containers" live in a
# temp dir and whose `run` starts a tiny stub engine. No GPU, no network, nothing on the real box is stopped: the normal
# upgrade (pull, restart on the new digest, acceptance), a release that fails acceptance (rolled back to the digest that was
# running, which serves again before the script exits), one that never answers (rolled back too), and a gufo container on
# the same port left alone. Everything lives in a temp dir; it needs the port below free.
#   tests/upgrade-strata.test.sh
# shellcheck disable=SC2034  # the check() expressions read OUT, RC ... through eval
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT=${STRATA_TEST_PORT:-18092}
fails=0
check() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }
OUTS=()
if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then echo "port $PORT is already in use" >&2; exit 2; fi

T=$(mktemp -d); S=$T/state
trap 'for p in "$S"/pid.*; do [ -f "$p" ] && kill "$(cat "$p")" 2>/dev/null; done; rm -rf "$T"' EXIT
export HOME="$T/home" PI_CODING_AGENT_DIR="$T/agent" STRATA_DATA="$T/data" STRATA_PORT=$PORT FAKE_STATE=$S
export STRATA_POLL_S=1 STRATA_WAIT_S=8 STRATA_MODEL=qwen36-q4 STRATA_IMAGE=ghcr.io/niko1221/strata:latest-test
mkdir -p "$HOME" "$PI_CODING_AGENT_DIR" "$STRATA_DATA" "$S" "$T/bin"

# the "engine" behind docker run: /v1/models, and a chat completion that answers OK and reports 1 token; FAKE_SLOW makes the
# decode check fail (a 1.5 s reply for 1 token = 0.7 t/s, below the 15 t/s floor)
cat > "$T/fake_strata.py" <<'PY'
import http.server, json, os, sys, time
port, slow = int(sys.argv[1]), os.environ.get("FAKE_SLOW") == "1"
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps({"data": [{"id": "stub-model"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if slow and b"Count from" in body: time.sleep(1.5)
        b = json.dumps({"choices": [{"message": {"content": "OK"}}], "usage": {"completion_tokens": 1}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
PY

# fake docker. The registry's tag -> digest is $S/tagref:<tag>; `pull` is what moves the LOCAL tag ($S/local:<tag>) and marks
# the digest present, so `image inspect <tag>` before a pull names the digest that is really running, as on a real box.
# One Strata container (c1: the stub engine's pid) and one unrelated container (g1: a sleep, "gufo on the same port").
# Every pull, stop and run is logged to $S/log.
cat > "$T/bin/docker" <<'DK'
#!/usr/bin/env bash
set -u
S=$FAKE_STATE; PORT=$STRATA_PORT; T=$(dirname "$S")
log(){ echo "$*" >> "$S/log"; }
present(){ [ -f "$S/present:${1##*@}" ]; }
local_digest(){ echo "${1%:*}@$(cat "$S/local:${1##*:}" 2>/dev/null || echo sha256:none)"; }
digest_of(){ case "$1" in *@sha256:*) echo "$1" ;; *) local_digest "$1" ;; esac; }
pid_of(){ cut -d'|' -f2 "$S/c.$1" 2>/dev/null; }
running(){ [ -n "$(pid_of c1)" ] && kill -0 "$(pid_of c1)" 2>/dev/null; }
case "$1" in
  pull) case "$2" in *@sha256:*) d=$2 ;; *) d=$(echo "${2%:*}@$(cat "$S/tagref:${2##*:}")"); echo "${d##*@}" > "$S/local:${2##*:}" ;; esac
        touch "$S/present:${d##*@}"; log "pull $2"; exit 0 ;;
  image) ref=${*: -1}; d=$(digest_of "$ref"); present "$d" && echo "$d"; exit 0 ;;  # image inspect -f FMT REF
  ps) case "$*" in *name=^strata-*) running && echo c1 ;; *publish=*) running && echo c1; [ -f "$S/c.g1" ] && kill -0 "$(pid_of g1)" 2>/dev/null && echo g1 ;; esac; exit 0 ;;
  inspect) id=${*: -1}; cut -d'|' -f1 "$S/c.$id" 2>/dev/null; exit 0 ;;  # inspect -f FMT ID -> the image it runs
  stop|rm) for id in "$@"; do case "$id" in c1|g1) if [ -n "$(pid_of "$id")" ] && kill -0 "$(pid_of "$id")" 2>/dev/null; then log "stop $id"; kill "$(pid_of "$id")"; fi; rm -f "$S/c.$id" ;; esac; done; exit 0 ;;
  run) case "$*" in *--entrypoint*) exit 0 ;; esac
       ref=${*: -1}; d=$(digest_of "$ref"); log "run $d"; echo $$ > "$S/pid.c1"; echo "$ref|$$" > "$S/c.c1"
       if [ ! -f "$S/c.g1" ]; then sleep 100000 & echo $! > "$S/pid.g1"; echo "ghcr.io/gufo-org/toolboxes/gufo-runtime:1|$!" > "$S/c.g1"; fi
       grep -qx "${d##*@}" "$S/dead" 2>/dev/null && exec sleep 100000
       grep -qx "${d##*@}" "$S/slow" 2>/dev/null && export FAKE_SLOW=1
       exec python3 "$T/fake_strata.py" "$PORT" ;;
  *) exit 0 ;;
esac
DK
chmod +x "$T/bin/docker"; export PATH="$T/bin:$PATH"
echo "sha256:aaaa" > "$S/tagref:latest-test"; docker pull ghcr.io/niko1221/strata:latest-test
up(){ curl -sf -m 2 "http://127.0.0.1:$PORT/v1/models" >/dev/null; }
wait_up(){ for _ in $(seq 1 40); do up && return 0; sleep 0.25; done; return 1; }
start(){ setsid nohup bash "$REPO/server/start-strata.sh" > "$T/start.log" 2>&1 < /dev/null & }
run(){ bash "$REPO/scripts/upgrade-strata.sh" "$@" 2>&1; }
runp(){ OUT=$(run); RC=$?; OUTS+=("$OUT"); }  # not OUT=$(run) at the call sites: RC must be the script's, and OUTS must survive the subshell
serving_digest(){ grep '^run ' "$S/log" | tail -1 | cut -d' ' -f2; }
gufo_alive(){ kill -0 "$(cat "$S/pid.g1")" 2>/dev/null; }

echo "== Strata aaaa serving, the tag moved to bbbb: pull, restart on the new digest, acceptance passes"
start; check "aaaa is up" 'wait_up'
echo "sha256:bbbb" > "$S/tagref:latest-test"
runp
check "exits 0 and says OK" '[ "$RC" = 0 ] && echo "$OUT" | grep -q "^OK: Strata image updated"'
check "the acceptance battery answered (with the image key sent)" 'echo "$OUT" | grep -q "answer: .OK."'
check "the new digest is what runs" '[ "$(serving_digest)" = "ghcr.io/niko1221/strata@sha256:bbbb" ] && up'
check "the old container was stopped by id, the new one started once" '[ "$(grep -c "^stop c1" "$S/log")" = 1 ] && [ "$(grep -c "^run " "$S/log")" = 2 ]'
check "the gufo container on the same port was not stopped" '! grep -q "^stop g1" "$S/log" && gufo_alive'
PINS=$PI_CODING_AGENT_DIR/harness-pins
check "floats on the tag, aaaa kept as the ./harness config rollback" 'grep -qx "STRATA_IMAGE=ghcr.io/niko1221/strata:latest-test" "$PINS" && grep -qx "PREV_STRATA_IMAGE=ghcr.io/niko1221/strata@sha256:aaaa" "$PINS"'

echo "== a release that fails the decode check is rolled back to the digest that was running"
echo "sha256:cccc" > "$S/tagref:latest-test"; echo "sha256:cccc" > "$S/slow"
runp
check "exit 1, says it rolls back to bbbb" '[ "$RC" = 1 ] && echo "$OUT" | grep -q "rolling back to sha256:bbbb"'
check "bbbb is serving again before the script exits" 'echo "$OUT" | grep -q "sha256:bbbb is serving again" && [ "$(serving_digest)" = "ghcr.io/niko1221/strata@sha256:bbbb" ] && up'
check "the failed image was run first, its container stopped after" 'grep -q "^run ghcr.io/niko1221/strata@sha256:cccc" "$S/log" && [ "$(grep -c "^pull " "$S/log")" = 4 ] && [ "$(grep -c "^stop c1" "$S/log")" = 3 ]'
rm -f "$S/slow"

echo "== a release that never answers is rolled back too"
echo "sha256:dddd" > "$S/tagref:latest-test"; echo "sha256:dddd" > "$S/dead"
runp
check "exit 1 after the wait, says it rolls back" '[ "$RC" = 1 ] && echo "$OUT" | grep -q "not answering after 8s" && echo "$OUT" | grep -q "rolling back to sha256:bbbb"'
check "bbbb is serving again" '[ "$(serving_digest)" = "ghcr.io/niko1221/strata@sha256:bbbb" ] && up'
rm -f "$S/dead"

echo "== a normal upgrade after that: eeee"
echo "sha256:eeee" > "$S/tagref:latest-test"
runp
check "upgrade to eeee succeeds" '[ "$RC" = 0 ] && [ "$(serving_digest)" = "ghcr.io/niko1221/strata@sha256:eeee" ] && up'
check "the failed upgrades left the rollback alone; eeee's is bbbb" 'grep -qx "PREV_STRATA_IMAGE=ghcr.io/niko1221/strata@sha256:bbbb" "$PINS" && [ "$(grep -c ^PREV_ "$PINS")" = 1 ]'
check "the gufo container was never stopped in any scenario" '! grep -q "^stop g1" "$S/log" && gufo_alive'

echo; [ "$fails" = 0 ] && echo "all checks passed" || { echo "$fails check(s) failed"; echo "--- outputs:"; for o in "${OUTS[@]}"; do echo "$o" | grep -v '^      waiting' | tail -12; echo ---; done; exit 1; }
