#!/usr/bin/env bash
# upgrade-strata.sh - update Strata with its own ./update.sh, restart it exactly the way it was running,
# run the acceptance battery, and roll back to the previous commit if anything fails.
#   scripts/upgrade-strata.sh      (starts Strata first if it is down: the restart reuses its folder and command line)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PORT=${STRATA_PORT:-8080}  # the launcher's own knob (server/start-strata.sh); the waits too, so the test can shorten them
URL=http://127.0.0.1:$PORT
POLL=${STRATA_POLL_S:-15} WAIT=${STRATA_WAIT_S:-1200}
mkdir -p "$HOME/.cache"; LOG="$HOME/.cache/strata-upgrade.log"  # per-user dir: a fixed /tmp name can be a planted symlink

# Strata in Docker: pull the newest tag of the same image, restart it the same way, roll back to the old digest on failure
IMAGE=$(bash "$REPO/server/start-strata.sh" --print-image)
if [ -n "$IMAGE" ]; then
  KEY=$(cat "${STRATA_DATA:-${XDG_CACHE_HOME:-$HOME/.cache}/local-ai-harness/strata-data}/harness-key" 2>/dev/null)
  AUTH=(-H "Authorization: Bearer $KEY")
  up(){ curl -sf -m 3 "${AUTH[@]}" "$URL/v1/models" >/dev/null 2>&1; }
  . "$REPO/scripts/lib-docker.sh"  # by port AND image name: gufo shares :8080 and is not ours to stop
  stop_it(){ local ids; ids=$(container_ids "$PORT" strata); [ -z "$ids" ] || docker stop -t 30 $ids >/dev/null 2>&1; }
  start_it(){ setsid nohup bash "$REPO/server/start-strata.sh" > "$1" 2>&1 < /dev/null & }  # the launcher recomputes the tag
  # what to roll back to: the image the running container was started on. After an earlier rollback that is the old digest while
  # the tag already points at the failed one; with nothing running, the tag as pulled locally
  CID=$(container_ids "$PORT" strata | head -1)
  OLD=$(docker image inspect -f '{{index .RepoDigests 0}}' "$([ -n "$CID" ] && docker inspect -f '{{.Config.Image}}' "$CID" || echo "$IMAGE")" 2>/dev/null)
  # every failed check lands here: stop the new image, pull and restart the OLD DIGEST (the launcher would recompute the
  # moving tag and run the new one again), and confirm it answers before this script exits
  rollback(){
    echo "FAIL: $1 - rolling back to ${OLD##*@}"
    [ -n "$OLD" ] || { echo "      no digest of the previous image is known - nothing to roll back to; Strata left as it is"; exit 1; }
    docker pull "$OLD" >/dev/null 2>&1 || { echo "      could not pull $OLD - Strata left stopped"; exit 1; }
    stop_it
    STRATA_IMAGE="$OLD" setsid nohup bash "$REPO/server/start-strata.sh" > "$LOG.rollback" 2>&1 < /dev/null &
    for ((t = 0; t < WAIT; t += POLL)); do sleep "$POLL"; up && { echo "      ${OLD##*@} is serving again (log: $LOG.rollback)"; exit 1; }; done
    echo "      ${OLD##*@} did not come back (see $LOG.rollback)"; exit 1
  }
  # a shard gone fails the new image and the old one alike: refuse before stopping anything
  if ! MISS=$(bash "$REPO/server/start-strata.sh" --check); then
    echo "FAIL: the model is missing files, so no image would load it - nothing stopped:"; echo "$MISS" | sed 's/^/      /'; exit 1
  fi
  echo "[1/3] pulling the newest $IMAGE"
  docker pull "$IMAGE" || { echo "FAIL: docker pull failed (offline, or the tag moved) - nothing changed"; exit 1; }
  echo "[2/3] restarting it (server/start-strata.sh)"
  stop_it
  start_it "$LOG.server"
  for ((t = 0; t < WAIT; t += POLL)); do sleep "$POLL"; up && break; done
  if ! up; then
    tail -5 "$LOG.server"
    rollback "not answering after ${WAIT}s (log: $LOG.server)"
  fi
  echo "[3/3] answer + decode check"
  # the image serves only with its key: without it the battery is a 401, and every upgrade "failed" and rolled back
  STRATA_KEY="$KEY" python3 "$REPO/scripts/engine-acceptance.py" "$URL" 15 \
    && echo "OK: Strata image updated and answering ($IMAGE)" || rollback "acceptance failed (log: $LOG.server)"
  exit 0
fi

PID=$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
if [ -z "$PID" ]; then
  echo "[0/4] Strata is not running - starting it (server/start-strata.sh) so the upgrade can restart it the same way"
  setsid nohup bash "$REPO/server/start-strata.sh" > "$LOG.start" 2>&1 < /dev/null &
  for ((t = 0; t < WAIT; t += POLL)); do
    sleep "$POLL"; [ -t 1 ] && printf '\r      waiting for Strata to load ... %ss (gives up at %ss)' "$t" "$WAIT"
    curl -sf -m 3 "$URL/v1/models" >/dev/null && break
  done
  [ -t 1 ] && printf '\r\033[K'
  PID=$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
fi
if [ -z "$PID" ] || ! tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null | grep -q 'serve/server.py'; then
  echo "FAIL: Strata is not running on :$PORT - start it first (the upgrade restarts it the same way)"; exit 1
fi
DIR=$(readlink "/proc/$PID/cwd")
mapfile -d '' CMD < "/proc/$PID/cmdline"
[ -f "$DIR/update.sh" ] && [ -d "$DIR/.git" ] || { echo "FAIL: $DIR is not a git clone of Strata - use its own UPDATE instead"; exit 1; }
[ -z "$(git -C "$DIR" status --porcelain --untracked-files=no)" ] || { echo "FAIL: $DIR has local changes to tracked files - commit or stash them first"; exit 1; }
# same ROCm redirect as scripts/install-strata.sh: the rebuild must use the wheels the install built with
SYS=${ROCM_PATH:-/opt/rocm}
[ -x "$SYS/bin/hipcc" ] && [ ! -x "$SYS/llvm/bin/clang++" ] && export ROCM_PATH="$DIR/.no-system-rocm"
OLD=$(git -C "$DIR" rev-parse HEAD)
CUR=$(git -C "$DIR" describe --tags 2>/dev/null || echo "${OLD:0:9}")
BRANCH=$(git -C "$DIR" symbolic-ref --short -q HEAD || echo main)

stop(){
  local p; p=$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  [ -n "$p" ] && kill "$p" 2>/dev/null
  for _ in $(seq 1 30); do ss -ltnH "sport = :$PORT" | grep -q . || return 0; sleep 2; done
  [ -n "$p" ] && kill -9 "$p" 2>/dev/null; sleep 2
}
# BROWSER=true: Strata's --open would pop a browser tab on every restart
start(){ (cd "$DIR" && BROWSER=true setsid nohup "${CMD[@]}" > "$1" 2>&1 < /dev/null &); }
wait_up(){ for ((t = 0; t < WAIT; t += POLL)); do sleep "$POLL"; [ -t 1 ] && printf '\r      waiting for Strata to load ... %ss (gives up at %ss)' "$t" "$WAIT"; curl -sf -m 3 "$URL/v1/models" >/dev/null && { [ -t 1 ] && printf '\r\033[K'; return 0; }; done; return 1; }
rollback(){
  echo "FAIL: $1 - rolling back to $CUR"
  stop
  git -C "$DIR" checkout -q -B "$BRANCH" "$OLD" \
    && (cd "$DIR" && sh ./setup.sh --update --yes < /dev/null > "$LOG.rollback" 2>&1) \
    || echo "      could not rebuild $CUR (log: $LOG.rollback)"
  start "$LOG.rollback-server"
  echo "      restarted on $CUR (log: $LOG.rollback-server)"
  exit 1
}

echo "[1/4] stopping Strata $CUR (pid $PID, $DIR)"
stop
echo "[2/4] ./update.sh: git pull + engine and packages (log: $LOG)"
(cd "$DIR" && sh ./update.sh --yes < /dev/null > "$LOG" 2>&1) || { tail -5 "$LOG"; rollback "update.sh failed (log: $LOG)"; }
NEW=$(git -C "$DIR" describe --tags 2>/dev/null || git -C "$DIR" rev-parse --short HEAD)
echo "[3/4] restarting on $NEW (loading the model takes a minute or two)"
start "$LOG.server"
wait_up || { tail -5 "$LOG.server"; rollback "not answering after ${WAIT}s (log: $LOG.server)"; }
echo "[4/4] answer + decode check"
python3 "$REPO/scripts/engine-acceptance.py" "$URL" 15 || rollback "acceptance"
echo "OK: Strata $CUR -> $NEW, running (previous commit kept as rollback point: git -C $DIR checkout $OLD)"
