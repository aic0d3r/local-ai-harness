#!/usr/bin/env bash
# upgrade-llamacpp.sh - pull a newer halo-box strix-llama.cpp image (server-<backend>-b<N>), restart server/start-qwen36.sh on
# it, run the acceptance battery, and only move the pin if everything passes. The pin (an image digest) is stored per user
# (harness-pins, scripts/lib-pins.sh); the tracked launcher is not edited.
#   scripts/upgrade-llamacpp.sh [b<N>]      (LLAMACPP_BACKEND=rocm upgrades the ROCm image)
# Releases only by default: halo-box tags images by build, not release (no :v1.1.0), so with no b<N> it does nothing while
# the pinned image's commit contains the latest GitHub release, else takes the newest build and checks that it contains it.
# An explicit b<N> installs that build, release or not.
# Rollback is automatic: any failed check stops the new server and restarts the old pin (if it was up).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
START="$REPO/server/start-qwen36.sh"
mkdir -p "$HOME/.cache"; LOG="$HOME/.cache/llamacpp-upgrade.log"  # per-user dir: a fixed /tmp name can be a planted symlink
R=halo-box/strix-llama.cpp
IMG=ghcr.io/$R
export PORT=${PORT:-8080}  # the launcher's own knob; the waits too, so a test can shorten them
URL=http://127.0.0.1:$PORT
POLL=${LLAMACPP_POLL_S:-15} WAIT=${LLAMACPP_WAIT_S:-900}
unset QWEN36_IMAGE  # the launcher must run the pin (or the candidate we hand it), not a stray override
. "$REPO/scripts/lib-pins.sh"
. "$REPO/scripts/lib-docker.sh"
up(){ curl -sf -m 3 "$URL/health" >/dev/null 2>&1; }

OLD_ID=$(container_ids "$PORT" strix-llama | head -1)
WAS_UP=$([ -n "$OLD_ID" ] && echo 1 || echo 0)
# restart the backend that was running (the image tells), else LLAMACPP_BACKEND
if [ "$WAS_UP" = 1 ] && [ "$(docker inspect -f '{{.Config.Image}}' "$OLD_ID")" = "$(LLAMACPP_BACKEND=rocm bash "$START" --print-image)" ]; then
  export LLAMACPP_BACKEND=rocm
fi
BACKEND=${LLAMACPP_BACKEND:-vulkan}; export LLAMACPP_BACKEND=$BACKEND
case "$BACKEND" in vulkan|rocm) ;; *) echo "FAIL: LLAMACPP_BACKEND=$BACKEND (vulkan or rocm)"; exit 1 ;; esac
PIN=LLAMACPP_$(echo "$BACKEND" | tr a-z A-Z)_IMAGE
DEF=$(grep -oE "^$(echo "$BACKEND" | tr a-z A-Z)_IMAGE=[^ ]+" "$START" | cut -d= -f2)
CUR_IMG=$(bash "$START" --print-image)  # the pin in force
CUR=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "$CUR_IMG" 2>/dev/null)
CUR=${CUR:-${CUR_IMG##*@}}

TAG="${1:-}"; TAG=${TAG##*-}  # server-vulkan-b13605 -> b13605
REL=
rev(){ docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1" 2>/dev/null; }
# contains COMMIT: COMMIT is the release $REL or after it (GitHub compare; offline or unknown = no)
contains(){ [ -n "$1" ] && curl -sf -m 10 "https://api.github.com/repos/$R/compare/$REL...$1" \
  | python3 -c 'import sys,json;sys.exit(json.load(sys.stdin)["status"] not in ("ahead","identical"))' 2>/dev/null; }
if [ -z "$TAG" ]; then
  REL=$(curl -sf -m 5 "https://api.github.com/repos/$R/releases/latest" | python3 -c 'import sys,json;print(json.load(sys.stdin)["tag_name"])' 2>/dev/null)
  [ -n "$REL" ] || { echo "FAIL: could not read the latest strix-llama.cpp release (offline?)"; exit 1; }
  contains "$(rev "$CUR_IMG")" && { echo "already on $CUR, which contains release $REL - nothing to do"; exit 0; }
  T=$(curl -sf -m 5 "https://ghcr.io/token?scope=repository:$R:pull" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
  TAG=$(curl -sf -m 5 -H "Authorization: Bearer $T" "https://ghcr.io/v2/$R/tags/list?n=1000" \
    | python3 -c 'import sys,json;print("\n".join(json.load(sys.stdin)["tags"]))' | sed -n "s/^server-$BACKEND-\(b[0-9]\+\)$/\1/p" | sort -V | tail -1)
  [ -n "$TAG" ] || { echo "FAIL: could not list strix-llama.cpp tags (offline?)"; exit 1; }
fi
case "$TAG" in b[0-9]*) ;; *) echo "FAIL: '$TAG' is not a build tag like b13605"; exit 1 ;; esac
[ "$TAG" = "$CUR" ] && { echo "already on $CUR ($BACKEND)${REL:+; no newer image build than this has release $REL yet} - nothing to do"; exit 0; }

# one big engine at a time: halogen, gufo or Strata on the GPU heap (or on :$PORT) would make the new image fail, the rollback too
[ -n "$(container_ids 8731 halogen)" ] && { echo "FAIL: halogen is running - stop it first (they share the GPU memory)"; exit 1; }
if [ "$WAS_UP" = 0 ] && { [ -n "$(docker ps -q --filter "publish=$PORT" 2>/dev/null)" ] || curl -s -m 3 -o /dev/null "$URL/"; }; then
  echo "FAIL: something else serves :$PORT - stop it first (./harness config, running)"; exit 1
fi
# a model file gone fails every image, the rollback's too: refuse before stopping anything (the launcher names the file)
bash "$START" --check || { echo "FAIL: the model is missing files (above), so no image would load it - nothing stopped"; exit 1; }

echo "[1/4] pulling $IMG:server-$BACKEND-$TAG (pinned: $CUR)"
docker pull "$IMG:server-$BACKEND-$TAG" >/dev/null 2>&1 || { echo "FAIL: pull $IMG:server-$BACKEND-$TAG"; exit 1; }
NEW=$(docker image inspect -f '{{index .RepoDigests 0}}' "$IMG:server-$BACKEND-$TAG" 2>/dev/null)
case "$NEW" in *@sha256:*) ;; *) echo "FAIL: no registry digest for $IMG:server-$BACKEND-$TAG"; exit 1 ;; esac
[ -z "$REL" ] || contains "$(rev "$NEW")" || { echo "FAIL: the newest image ($TAG) does not contain release $REL yet (or GitHub did not say) - nothing stopped"; exit 1; }
echo "      upgrading $CUR -> $TAG"

stop_it(){ local ids; ids=$(container_ids "$PORT" strix-llama); [ -z "$ids" ] || docker stop -t 30 $ids >/dev/null 2>&1; }
wait_up(){
  for ((t = POLL; t <= WAIT; t += POLL)); do
    sleep "$POLL"; [ -t 1 ] && printf '\r      waiting for llama.cpp to load ... %ss (gives up at %ss)' "$t" "$WAIT"; up && break
  done
  [ -t 1 ] && printf '\r\033[K'; up
}
rollback(){
  echo "FAIL: $1 - rolling back to $CUR"
  stop_it
  if [ "$WAS_UP" = 1 ]; then
    setsid nohup bash "$START" > "$LOG.rollback" 2>&1 < /dev/null &
    echo "      restarting on $CUR (log: $LOG.rollback); pin unchanged"
    # wait for it: whoever sees this script exit (the wizard) must not start a second server on the same port
    wait_up && echo "      $CUR is serving again" || echo "      $CUR did not come back (see $LOG.rollback)"
  else echo "      llama.cpp was not running before; left stopped. pin unchanged"; fi
  exit 1
}

echo "[2/4] restarting on $TAG (loading the model can take minutes)"
stop_it
rm -f "$LOG"
QWEN36_IMAGE="$NEW" setsid nohup bash "$START" > "$LOG" 2>&1 < /dev/null &
wait_up || { tail -5 "$LOG"; rollback "not answering after ${WAIT}s (log: $LOG)"; }
echo "      answering on :$PORT"

echo "[3/4] answer + decode check"
python3 "$REPO/scripts/engine-acceptance.py" "$URL" 15 || rollback "acceptance"

echo "[4/4] updating pin $CUR -> $TAG"
pin_set "$PIN" "$DEF" "$NEW" || { echo "FAIL: could not write $PINS - pin unchanged (still running $TAG until the next restart)"; exit 1; }
echo "OK: validated and pinned $BACKEND to $TAG in $PINS (running now). A harness update that ships a newer reviewed pin replaces it."
