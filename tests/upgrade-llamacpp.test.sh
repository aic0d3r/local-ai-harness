#!/usr/bin/env bash
# scripts/upgrade-llamacpp.sh against a fake docker (a script in PATH whose images and container live in a temp dir and
# whose `run` starts a tiny stub engine) and a fake curl for GitHub/ghcr. No GPU, no network, nothing on the real box touched:
# a passing upgrade (pinned), one that fails the decode check and one that never answers (both rolled back to the pin, serving
# again, pin unchanged), and the releases-only default (nothing while the pin contains the latest release).
#   tests/upgrade-llamacpp.test.sh      (needs the port below free)
# shellcheck disable=SC2034  # the check() expressions read OUT, RC through eval
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT=${LLAMACPP_TEST_PORT:-18093}
fails=0
check() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }
if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then echo "port $PORT is already in use" >&2; exit 2; fi

T=$(mktemp -d); S=$T/state
trap '[ -f "$S/pid" ] && kill "$(cat "$S/pid")" 2>/dev/null; rm -rf "$T"' EXIT
export HOME="$T/home" PI_CODING_AGENT_DIR="$T/agent" PORT FAKE_STATE=$S LLAMACPP_POLL_S=1 LLAMACPP_WAIT_S=8
export LLAMACPP_FAMILY=qwen36 QWEN36_GGUF="$T/model.gguf"
mkdir -p "$HOME" "$PI_CODING_AGENT_DIR" "$S" "$T/bin"; touch "$QWEN36_GGUF"
PINS="$PI_CODING_AGENT_DIR/harness-pins"
IMG=ghcr.io/halo-box/strix-llama.cpp
DEF=$(bash "$REPO/server/start-qwen36.sh" --print-image)
echo b13600 > "$S/label:${DEF##*@}"

# the engine: answers OK reporting 1 token; FAKE_SLOW makes the decode check fail (1.5 s for 1 token)
cat > "$T/fake.py" <<'PY'
import http.server, json, os, sys, time
port, slow = int(sys.argv[1]), os.environ.get("FAKE_SLOW") == "1"
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, o):
        b = json.dumps(o).encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self.reply({"data": [{"id": "qwen36-q4"}]})
    def do_POST(self):
        if slow and b"Count from" in self.rfile.read(int(self.headers.get("Content-Length", 0))): time.sleep(1.5)
        self.reply({"choices": [{"message": {"content": "OK"}}], "usage": {"completion_tokens": 1}})
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
PY

# fake docker: registry tag -> digest in $S/tagref:<tag>, a digest's build label in $S/label:<digest>, one container "c1"
# ($S/c = the image it runs). Every pull, stop and run is logged to $S/log.
cat > "$T/bin/docker" <<'DK'
#!/usr/bin/env bash
S=$FAKE_STATE; T=$(dirname "$S")
log(){ echo "$*" >> "$S/log"; }
alive(){ [ -f "$S/pid" ] && kill -0 "$(cat "$S/pid")" 2>/dev/null; }
case "$1" in
  pull) d=$(cat "$S/tagref:${2##*:}") || exit 1; log "pull $2"; echo "${2%:*}@$d" > "$S/ref:${2##*:}"; exit 0 ;;
  image) ref=${*: -1}; case "$ref" in *@*) d=$ref ;; *) d=$(cat "$S/ref:${ref##*:}" 2>/dev/null) ;; esac
         case "$*" in *RepoDigests*) echo "$d" ;; *revision*) echo "rev-${d##*:}" ;; *) cat "$S/label:${d##*@}" 2>/dev/null ;; esac; exit 0 ;;
  ps) alive && echo c1; exit 0 ;;
  inspect) cat "$S/c"; exit 0 ;;
  stop) alive && { log "stop c1"; kill "$(cat "$S/pid")"; rm -f "$S/pid"; sleep 0.2; }; exit 0 ;;
  run) for a in "$@"; do case "$a" in ghcr.io/*) ref=$a ;; esac; done
       log "run $ref"; echo "$ref" > "$S/c"; echo $$ > "$S/pid"
       grep -qx "${ref##*@}" "$S/dead" 2>/dev/null && exec sleep 100000
       grep -qx "${ref##*@}" "$S/slow" 2>/dev/null && export FAKE_SLOW=1
       exec python3 "$T/fake.py" "$PORT" ;;
esac
DK
# fake curl: the latest release is $S/release, the image commits that contain it are listed in $S/contains (commit of digest
# sha256:X = rev-X), the ghcr tags are $S/tags; everything else (the stub engine) goes to the real curl
REAL_CURL=$(command -v curl)
cat > "$T/bin/curl" <<DK
#!/usr/bin/env bash
S=$S
case "\$*" in
  *releases/latest*) echo "{\"tag_name\": \"\$(cat \$S/release)\"}" ;;
  *compare/*) u=\${*: -1}; grep -qx "\${u##*...}" \$S/contains 2>/dev/null && echo '{"status": "ahead"}' || echo '{"status": "behind"}' ;;
  *ghcr.io/token*) echo '{"token": "t"}' ;;
  *tags/list*) python3 -c 'import json,sys;print(json.dumps({"tags": open(sys.argv[1]).read().split()}))' \$S/tags ;;
  *) exec $REAL_CURL "\$@" ;;
esac
DK
chmod +x "$T/bin/docker" "$T/bin/curl"; export PATH="$T/bin:$PATH"
up(){ curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null; }
runp(){ OUT=$(bash "$REPO/scripts/upgrade-llamacpp.sh" "$@" 2>&1); RC=$?; }
serving(){ grep '^run ' "$S/log" | tail -1 | cut -d' ' -f2; }
release(){ echo "sha256:$2" > "$S/tagref:server-vulkan-$1"; echo "$1" > "$S/label:sha256:$2"; }

echo "== running the default b13600, upgrade to b13605: pull, restart, acceptance, pin"
setsid nohup bash "$REPO/server/start-qwen36.sh" > "$T/start.log" 2>&1 < /dev/null &
for _ in $(seq 1 40); do up && break; sleep 0.25; done
check "b13600 is up" 'up && [ "$(serving)" = "$DEF" ]'
release b13605 bbbb; runp server-vulkan-b13605
check "exits 0 and says OK" '[ "$RC" = 0 ] && echo "$OUT" | grep -q "^OK: strix-llama.cpp .* → b13605 (vulkan), running and pinned"'
check "the new digest runs and is pinned" '[ "$(serving)" = "$IMG@sha256:bbbb" ] && up && grep -qx "LLAMACPP_VULKAN_IMAGE=$IMG@sha256:bbbb" "$PINS"'
check "the launcher now runs the pin" '[ "$(bash "$REPO/server/start-qwen36.sh" --print-image)" = "$IMG@sha256:bbbb" ]'
runp b13605; check "same build again: nothing to do" '[ "$RC" = 0 ] && echo "$OUT" | grep -q "already on b13605"'

echo "== b13610 fails the decode check: rolled back to the pin"
release b13610 cccc; echo sha256:cccc > "$S/slow"; runp b13610
check "exit 1, rolls back to b13605, serving again" '[ "$RC" = 1 ] && echo "$OUT" | grep -q "rolling back to b13605" && echo "$OUT" | grep -q "b13605 is serving again"'
check "bbbb runs, the pin did not move" '[ "$(serving)" = "$IMG@sha256:bbbb" ] && up && grep -qx "LLAMACPP_VULKAN_IMAGE=$IMG@sha256:bbbb" "$PINS"'

echo "== b13620 never answers: rolled back too"
release b13620 dddd; echo sha256:dddd > "$S/dead"; runp b13620
check "exit 1 after the wait, bbbb serving again" '[ "$RC" = 1 ] && echo "$OUT" | grep -q "not answering after 8s" && [ "$(serving)" = "$IMG@sha256:bbbb" ] && up'

echo "== releases only (no build given): v1.1.0 is in the pinned bbbb, v1.2.0 is not"
echo v1.1.0 > "$S/release"; echo rev-bbbb > "$S/contains"; printf 'server-vulkan-b13605\nserver-vulkan-b13630\nserver-rocm-b13700\n' > "$S/tags"
n=$(grep -c '^pull' "$S/log"); runp
check "pin contains the release: nothing to do, nothing pulled" '[ "$RC" = 0 ] && echo "$OUT" | grep -q "contains release v1.1.0 - nothing to do" && [ "$(grep -c "^pull" "$S/log")" = "$n" ]'
echo v1.2.0 > "$S/release"; : > "$S/contains"; release b13630 eeee; runp
check "newest build lacks v1.2.0: refused, nothing stopped" '[ "$RC" = 1 ] && echo "$OUT" | grep -q "does not contain release v1.2.0" && [ "$(serving)" = "$IMG@sha256:bbbb" ] && up'
echo rev-eeee > "$S/contains"; runp
check "newest build contains v1.2.0: upgraded to vulkan b13630 and pinned" '[ "$RC" = 0 ] && [ "$(serving)" = "$IMG@sha256:eeee" ] && grep -qx "LLAMACPP_VULKAN_IMAGE=$IMG@sha256:eeee" "$PINS"'

echo "== a missing model file: refused before anything is stopped"
rm "$QWEN36_GGUF"; n=$(grep -c '^stop' "$S/log"); runp b13620
check "exit 1, nothing stopped" '[ "$RC" = 1 ] && echo "$OUT" | grep -q "nothing stopped" && [ "$(grep -c "^stop" "$S/log")" = "$n" ] && up'

echo; [ "$fails" = 0 ] && echo "all checks passed" || { echo "$fails check(s) failed"; echo "--- last output:"; echo "$OUT" | tail -15; exit 1; }
