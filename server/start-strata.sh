#!/usr/bin/env bash
# Start Strata (github.com/Niko1221/Strata) on the saved weights answer. Both answers run UD-Q4_K_XL:
# stock = Unsloth's GGUF that gufo shares (scripts/download-stock.sh), abliterated = gufo's orca-uncensored shards.
#
# Docker (default when docker is installed and the card has a ready-made image: gfx1151 Strix Halo, other RDNA 3/4,
# NVIDIA 575+/580+) runs ghcr.io/niko1221/strata:<release>-<flavor> instead of compiling Strata and installing its
# ~10 GB ROCm wheels. Its /data volume (packs, settings, key) is $STRATA_DATA, mounted from the host so the pack and
# the weights marker survive restarts. A source install is used whenever one exists ($STRATA_DIR, ~/coding/Strata,
# ~/Strata, ~/strata, ~/src/Strata), and always with STRATA_SOURCE=1.
# Strata keys its repacked dense weights by model name only, so <checkout or $STRATA_DATA>/.harness-weights records
# whose weights the pack was built from; starting on the other answer drops that pack so Strata rebuilds it.
# The Docker image always serves with a key (docs/DOCKER.md): <$STRATA_DATA>/harness-key is passed as API_KEY, and pi
# gets the same key from there (setup/harness.py strata_key), so probes and the agent both authenticate.
# No run script for the saved model: the newest run-<model>.sh its setup wrote, else its ./setup.sh.
# One big engine at a time: stop halogen/gufo first (the wizard does that for you).
#   --print-dir   the source checkout, or the Docker data dir
#   --print-run   what starts the saved model and weights answer, empty when that model is not installed yet
#   --print-image the Docker image, empty for a source install
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PI_DIR=${PI_CODING_AGENT_DIR:-${PI_HOME:-$HOME/.pi}/agent}
XDG=${XDG_CACHE_HOME:-$HOME/.cache}
DATA=${STRATA_DATA:-$XDG/local-ai-harness/strata-data}  # Strata's /data: packs, settings, API key
PORT=${STRATA_PORT:-8080}
WEIGHTS="${STRATA_WEIGHTS:-$(cat "$PI_DIR/harness-weights" 2>/dev/null)}"
WEIGHTS=${WEIGHTS:-stock}
# the saved model answer (./harness config -> model, or STRATA_MODEL): a catalog id with its own Strata run script, else
# Flash-Next on the Unsloth layout (OWN=0), the only one the stock/abliterated answer applies to
MODEL="${STRATA_MODEL:-$(cat "$PI_DIR/harness-model" 2>/dev/null)}"; MODEL=${MODEL:-flash-next}
RUN=$(python3 "$HERE/../setup/harness.py" model-info "$MODEL" run 2>/dev/null) && OWN=1 || { OWN=0; RUN=run-unsloth-ud-q4_k_xl.sh; }
for d in "${STRATA_DIR:-}" "$HOME/coding/Strata" "$HOME/Strata" "$HOME/strata" "$HOME/src/Strata"; do
  [ -n "$d" ] && [ -f "$d/setup.py" ] && [ -f "$d/update.sh" ] && { DIR=$d; break; }
done

# the ready-made image a card needs (docs/DOCKER.md), or nothing. Read from the kernel, like the wizard's own hw: the
# image needs no ROCm userspace on the host, and asking rocminfo would require one.
flavor() {
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    local drv; drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | cut -d. -f1)
    if [ "${drv:-0}" -ge 580 ] 2>/dev/null; then echo cuda13; else echo cuda12; fi
    return
  fi
  case "$(cat /sys/class/kfd/kfd/topology/nodes/*/properties 2>/dev/null | awk '/gfx_target_version/{if ($2 > 0) {print $2; exit}}')" in
    1105*) echo rocm-gfx1151 ;;
    12*) echo rocm-gfx120x ;;
    1100*|1101*|1102*) echo rocm-gfx110x ;;
  esac
}
IMAGE=${STRATA_IMAGE:-}
if [ -z "$IMAGE" ] && command -v docker >/dev/null 2>&1; then
  F=$(flavor) && [ -n "$F" ] && IMAGE="ghcr.io/niko1221/strata:${STRATA_TAG:-latest-$F}"
fi
# shortcut: no source install is an error only when neither route can work; say which knob fixes it
if [ -z "${DIR:-}" ] && [ -z "$IMAGE" ]; then
  echo "Strata not found (looked in \$STRATA_DIR, ~/coding/Strata, ~/Strata, ~/strata, ~/src/Strata) and no ready-made image" >&2
  echo "for this card: install it with scripts/install-strata.sh (or ./setup.sh, which offers it), or set STRATA_DIR," >&2
  echo "STRATA_IMAGE (ghcr.io/niko1221/strata:<release>-<flavor>, docs/DOCKER.md) or STRATA_SOURCE=1" >&2
  exit 1
fi
[ -n "${DIR:-}" ] || [ "${STRATA_SOURCE:-0}" = 1 ] && IMAGE=""  # a source install wins over the image

# Flash-Next's GGUF for the saved weights (shard 1; the Docker route builds its pack from it) and its shards not on disk
shard1() { GUFO_QUANT=UD-Q4_K_XL GUFO_WEIGHTS=$WEIGHTS bash "$HERE/start-gufo-flashnext.sh" --print-model; }
missing_shards() {
  local s1 n i f; s1=$(shard1); n=$(echo "$s1" | sed -nE 's/.*-00001-of-0*([0-9]+)\.gguf$/\1/p')
  for i in $(seq 1 "${n:-1}"); do f=${s1/-00001-of-/-$(printf %05d "$i")-of-}; [ -f "$f" ] || echo "$f"; done
}

case "${1:-}" in
  # upgrade-strata.sh's preflight: the shards the Docker route reads (a source install runs its finished pack)
  --check) [ -n "$IMAGE" ] && [ "$OWN" = 0 ] && missing_shards | grep . && exit 1; exit 0 ;;
  --print-dir) echo "${DIR:-$DATA}"; exit 0 ;;
  --print-image) echo "$IMAGE"; exit 0 ;;
  --print-run)
    if [ -n "$IMAGE" ]; then docker image inspect "$IMAGE" >/dev/null 2>&1 && echo "docker: $IMAGE"; exit 0; fi
    [ -f "$DIR/$RUN" ] && { [ "$OWN" = 1 ] || [ "$(cat "$DIR/.harness-weights" 2>/dev/null || echo abliterated)" = "$WEIGHTS" ]; } && echo "$DIR/$RUN"
    exit 0 ;;
esac

# -- a source install: its own run script against its own packs
if [ -z "$IMAGE" ]; then
  BUILT=$(cat "$DIR/.harness-weights" 2>/dev/null || echo abliterated)
  if [ "$OWN" = 0 ] && [ -f "$DIR/$RUN" ] && [ "$BUILT" != "$WEIGHTS" ]; then
    # rebuild in place: the other answer's pack must go first, Strata reuses a finished pack by name
    PACK=$(python3 -c 'import json,sys;a=json.load(open(sys.argv[1]))["args"];print(a[a.index("--pack")+1])' "$DIR/strata-unsloth-ud-q4_k_xl.json" 2>/dev/null)
    case "$PACK" in */packs/unsloth-ud-q4_k_xl) rm -rf "$PACK" ;; *) echo "Strata in $DIR is built from the $BUILT weights, your saved answer is $WEIGHTS: scripts/install-strata.sh rebuilds it" >&2; exit 1 ;; esac
    printf '%s\n' "$WEIGHTS" > "$DIR/.harness-weights"
  fi
  if [ "$OWN" = 1 ]; then
    [ -f "$DIR/$RUN" ] || { echo "Strata has no $RUN for the saved model ($MODEL): scripts/install-strata.sh sets it up" >&2; exit 1; }
  else
    [ -f "$DIR/$RUN" ] || RUN=$(ls -t "$DIR"/run-*.sh 2>/dev/null | head -1)
  fi
  # Strix Halo: every expert in the GPU pool when they fit (setup writes a slower RAM-budget mode); idempotent, a no-op elsewhere
  CFG=$(grep -o '"--config" "[^"]*"' "$DIR/$RUN" 2>/dev/null | cut -d'"' -f4)
  [ -n "$CFG" ] && [ -x "$DIR/.venv/bin/python" ] && "$DIR/.venv/bin/python" "$HERE/../scripts/strata-uma.py" "$CFG"
  cd "$DIR" || exit 1
  export BROWSER=true  # its --open would pop a browser tab on every start
  [ -n "$RUN" ] && exec sh "$RUN"
  exec sh ./setup.sh --yes
fi

# -- the ready-made image: one container, everything it writes goes to $DATA
mkdir -p "$DATA" || exit 1
KEYFILE=$DATA/harness-key
[ -s "$KEYFILE" ] || ( umask 077; python3 -c 'import secrets; print(secrets.token_hex(24))' > "$KEYFILE" ) || exit 1
# the key a start left inside the volume wins (Strata keeps it root-only there, so read it through a container): a Strata
# started by hand or by its own installer has its own key, and the harness has to send that one, not the file's
PREV=$(docker run --rm -v "$DATA":/data --entrypoint cat "$IMAGE" /data/state/api-key 2>/dev/null | tr -d '\r\n')
[ -n "$PREV" ] && printf '%s\n' "$PREV" > "$KEYFILE"
# a start while one is already running (the wizard retries, ./harness up runs again) must not fail on the name: follow it
# instead, and clear a container an earlier crash left behind
if [ -n "$(docker ps -q --filter "name=^strata-$PORT$")" ]; then
  echo "  Strata is already running (container strata-$PORT): following its log; stop it with docker stop strata-$PORT"
  exec docker logs -f "strata-$PORT"
fi
docker rm -f "strata-$PORT" >/dev/null 2>&1
ARGS=(--rm -i --name "strata-$PORT"
  --ulimit memlock=-1:-1 --shm-size 2g --stop-timeout 120
  -p "127.0.0.1:$PORT:8080" -v "$DATA":/data -e "API_KEY=$(cat "$KEYFILE")"
  -e "PARALLEL=${STRATA_PARALLEL:-1}")  # one slot: the MoE prompt chunk grows 1024 -> 4096, measured 479 -> 692 t/s prefill
[ -n "${STRATA_SET:-}" ] && ARGS+=(-e "STRATA_SET=$STRATA_SET")  # advanced engine settings (strata launch --set), e.g. prefill=4096
if case "$IMAGE" in *cuda*) true ;; *) false ;; esac; then
  ARGS+=(--gpus all)
else
  ARGS+=(--device /dev/kfd --device /dev/dri
    --group-add "${RENDER_GID:-$(stat -c %g /dev/dri/renderD128 2>/dev/null || echo 44)}"
    --group-add "${VIDEO_GID:-$(stat -c %g /dev/dri/card0 2>/dev/null || echo 44)}"
    --security-opt seccomp=unconfined --ipc=host)
fi
if [ "$OWN" = 1 ]; then  # a catalog model Strata downloads itself (qwen36-q4 etc.)
  read -r FAM SIZE <<< "$(python3 "$HERE/../setup/harness.py" model-info "$MODEL" strata)" || exit 1
  [ -n "${SIZE:-}" ] || { echo "Strata has no image model for $MODEL: set it up with its own setup.sh" >&2; exit 1; }
  ARGS+=(-e "FAMILY=$FAM" -e "MODEL=$SIZE")
else  # Flash-Next from the GGUF folder gufo already has, read-only; the pack is built in $DATA
  SHARD1=$(shard1); MISS=$(missing_shards | tr '\n' ' ')
  if [ -n "$MISS" ]; then
    if [ "$WEIGHTS" = abliterated ]; then echo "the abliterated weights are not all on disk (missing: $MISS): scripts/download-abliterated.sh gufo" >&2
    else echo "the stock weights are not all on disk (missing: $MISS): scripts/download-stock.sh" >&2; fi
    exit 1
  fi
  if [ "$(cat "$DATA/harness-weights" 2>/dev/null)" != "$WEIGHTS" ]; then
    echo "  rebuilding Strata's pack for the $WEIGHTS weights (Strata reuses a finished pack by name)"
    rm -rf "$DATA/Strata-data/packs/unsloth-ud-q4_k_xl" 2>/dev/null
  fi
  printf '%s\n' "$WEIGHTS" > "$DATA/harness-weights"
  ARGS+=(-e FAMILY=unsloth -e MODEL=UD-Q4_K_XL -e MODEL_PATH=/models -v "$(dirname "$SHARD1")":/models:ro)
fi
echo "  Strata in Docker: $IMAGE (data: $DATA, key: $KEYFILE, port: 127.0.0.1:$PORT)"
exec docker run "${ARGS[@]}" "$IMAGE"
