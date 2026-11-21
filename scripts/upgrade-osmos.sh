#!/usr/bin/env bash
# upgrade-osmos.sh - update libosmos (the osmos engine, used for DeepSeek-V4.1-Flash): check out a release, rebuild,
# restart osmos on it, run the acceptance battery, and roll back to the previous commit if anything fails.
#   scripts/upgrade-osmos.sh [tag]      (default: the latest release; starts osmos on it whether or not it was running)
# The pack on disk (~141 GB) is never touched: a release only changes the engine, and its installer's layout changes are
# opt-in (--ctx), so an existing pack's osmos.json stays valid. Your own uncommitted edits to tracked files stay in place:
# git carries them over, and refuses the checkout (before anything is stopped) if the release changes the same lines.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
START="$REPO/server/start-osmos.sh"
PORT=${OSMOS_PORT:-8091}
URL=http://127.0.0.1:$PORT
mkdir -p "$HOME/.cache"; LOG="$HOME/.cache/osmos-upgrade.log"  # per-user dir: a fixed /tmp name can be a planted symlink

DIR=$(bash "$START" --print-dir)
[ -d "$DIR/.git" ] && [ -f "$DIR/Makefile" ] || { echo "FAIL: $DIR is not a libosmos git checkout (set OSMOS_DIR)"; exit 1; }
git -C "$DIR" fetch -q --tags origin || { echo "FAIL: could not reach the libosmos remote (offline?)"; exit 1; }

OLD=$(git -C "$DIR" rev-parse HEAD)
CUR=$(git -C "$DIR" describe --tags 2>/dev/null || echo "${OLD:0:9}")
BRANCH=$(git -C "$DIR" symbolic-ref --short -q HEAD || true)
TAG="${1:-}"
if [ -z "$TAG" ]; then  # the latest release, else the highest vX.Y.Z tag
  TAG=$(curl -sf -m 15 https://api.github.com/repos/mrsirg97-rgb/libosmos/releases/latest | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])' 2>/dev/null)
  [ -n "$TAG" ] || TAG=$(git -C "$DIR" tag -l 'v[0-9]*' | sort -V | tail -1)
fi
git -C "$DIR" rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null || { echo "FAIL: no tag $TAG in $DIR"; exit 1; }
NEWC=$(git -C "$DIR" rev-parse "$TAG^{commit}")
if [ "$NEWC" = "$OLD" ]; then echo "already on $CUR - nothing to do"; exit 0; fi
# a branch that holds commits the release does not contain would be rewound: keep those out of an automatic upgrade
if [ -n "$BRANCH" ] && ! git -C "$DIR" merge-base --is-ancestor "$OLD" "$NEWC"; then
  echo "FAIL: $DIR ($BRANCH) has commits that $TAG does not contain - update it by hand"; exit 1
fi
# a pack with a file gone (a cleaned HF cache, say) fails on every engine, the rollback's too: refuse before stopping it
if ! MISSING=$(bash "$START" --check); then
  echo "FAIL: the pack is missing files, so no version would start - nothing stopped:"; echo "$MISSING" | sed 's/^/      /'
  echo "      run server/start-osmos.sh once (its installer fetches them again), then re-run this"; exit 1
fi
DIRTY=$(git -C "$DIR" status --porcelain --untracked-files=no)
[ -n "$DIRTY" ] && echo "      keeping your local edits: $(echo "$DIRTY" | awk '{print $2}' | tr '\n' ' ')"

PIDS(){ pgrep -f "$DIR/bin/osmos-(api|serve)" 2>/dev/null; }
stop(){
  local p; p=$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  [ -n "$p" ] && kill "$p" 2>/dev/null
  for _ in $(seq 1 30); do [ -z "$(PIDS)" ] && ! ss -ltnH "sport = :$PORT" | grep -q . && return 0; sleep 2; done
  # shellcheck disable=SC2046
  kill -9 $(PIDS) 2>/dev/null; sleep 2  # osmos-serve holds ~91 GiB: it must be gone before the next load
}
build(){  # what osmos_install.py runs; the same NPU choice as the existing build (the NPU build links XRT)
  local npu=(); ldd "$DIR/bin/osmos-serve" 2>/dev/null | grep -q xrt && npu=(NPU=1)
  (cd "$DIR" && git submodule update --init && make -j "${npu[@]}" && make -j serve "${npu[@]}" && make -j api "${npu[@]}") > "$1" 2>&1
}
start(){ setsid nohup bash "$START" > "$1" 2>&1 < /dev/null & }
POLL=${OSMOS_POLL_S:-15}  # seconds between health checks (the tests use 1)
wait_up(){  # /health is 503 while osmos loads its 91 GiB; a launcher that exits early is a failed start
  local pid=$1 i
  for i in $(seq 1 $((1200 / POLL))); do
    sleep "$POLL"; [ -t 1 ] && printf '\r      waiting for osmos to load ... %ss (gives up at 1200s)' $((i*POLL))
    curl -sf -m 3 "$URL/health" >/dev/null && { [ -t 1 ] && printf '\r\033[K'; return 0; }
    kill -0 "$pid" 2>/dev/null || break
  done
  [ -t 1 ] && printf '\r\033[K'; return 1
}
rollback(){
  echo "FAIL: $1 - rolling back to $CUR"
  stop
  { [ -n "$BRANCH" ] && git -C "$DIR" checkout -q -B "$BRANCH" "$OLD" || git -C "$DIR" checkout -q "$OLD"; } 2>/dev/null \
    && build "$LOG.rollback" || echo "      could not rebuild $CUR (log: $LOG.rollback)"
  start "$LOG.rollback-server"; local pid=$!
  echo "      restarting on $CUR (log: $LOG.rollback-server)"
  # wait for it: whoever sees this script exit (the wizard) must not start a second server on the same port
  wait_up "$pid" && echo "      $CUR is serving again" || echo "      $CUR did not come back (see $LOG.rollback-server)"
  exit 1
}

echo "[1/5] checking out $TAG in $DIR (running: $CUR)"
# before anything is stopped: a conflict with your local edits ends the upgrade with the old server still up
if [ -n "$BRANCH" ]; then git -C "$DIR" checkout -q -B "$BRANCH" "$TAG"; else git -C "$DIR" checkout -q "$TAG"; fi \
  || { echo "FAIL: git cannot check out $TAG over your local edits - commit or stash them, then re-run"; exit 1; }
echo "[2/5] stopping osmos, building $TAG (log: $LOG)"
stop
build "$LOG" || { tail -5 "$LOG"; rollback "build failed (log: $LOG)"; }
GOT=$("$DIR/bin/osmos-api" --version 2>/dev/null)
case "$GOT" in *"${TAG#v}"*) ;; *) rollback "built binary says '${GOT:-nothing}', wanted ${TAG#v}" ;; esac
echo "[3/5] starting on $TAG (loading ~91 GiB takes a few minutes)"
start "$LOG.server"; SPID=$!
wait_up "$SPID" || { tail -5 "$LOG.server"; rollback "not answering after 20 min (log: $LOG.server)"; }
echo "      serving $GOT"
echo "[4/5] answer + decode check (the first request fills the expert cache: slow, so it is not timed)"
python3 "$REPO/scripts/engine-acceptance.py" "$URL" 10 || rollback "acceptance"
echo "[5/5] done"
echo "OK: osmos $CUR -> $TAG, running (previous commit kept as rollback point: git -C $DIR checkout $OLD)"
