#!/usr/bin/env bash
# Start llama.cpp for Qwen3.6-35B-A3B (UD-Q4_K_XL) from the halo-box Strix Halo image - one docker run, nothing to build.
# Why this engine for this model (measured here 2026-11-16, bench-v1 protocol, 131072 ctx, one engine at a time):
#   llama.cpp + its MTP head   85.3 t/s decode, 2,048 t/s prefill, 85-88% of drafts accepted
#   llama.cpp, no MTP          59.8 t/s decode, 1,017 t/s prefill
#   Strata (Docker image)      51.7 t/s decode,   601 t/s prefill (its MTP loads but proposes 0 drafts)
#   gufo                       cannot load qwen35moe at all
# The drafter is the model's own MTP layer (blk.40.nextn.* in the GGUF), so no second model and no extra download:
# --spec-type draft-mtp is llama.cpp's MTP path (upstream PR #22673) and --spec-draft-n-max 2 was the best of 2 / 3 here.
# Image: ghcr.io/halo-box/strix-llama.cpp:server-vulkan-b13600 (Halobox v1.1.0 + 11 commits), the Vulkan build of github.com/halo-box/strix-llama.cpp (the Strix Halo fork that absorbed the older
# strix-halo-llamacpp work); it bundles Mesa RADV, so the host needs only the amdgpu driver. LLAMACPP_BACKEND=rocm runs
# the same build's ROCm image instead (v1.1.0 notes: faster prefill, slower decode than Vulkan on Strix Halo).
# The GGUF: $QWEN36_GGUF, else a known models folder, else the copy Strata's Docker volume already downloaded (no move).
#   QWEN36_IMAGE, QWEN36_CTX (default 131072), QWEN36_SPEC (draft tokens, default 2), QWEN36_BATCH (default 2048),
#   PORT (default 8080), QWEN36_ALT_MODEL (serve another GGUF file name, for the 27B/Flash-Next llama.cpp route)
#   --print-model   the GGUF it serves (empty when none is on disk)
#   --print-image   the image it runs
#   --print-lora    the abliteration LoRA path (Flash-Next only; may not exist yet)
#   --check         exit 0 when every model file it would serve is on disk
# Qwen3.8-Flash-Next instead when the wizard's saved model is flash-next (harness-model; LLAMACPP_FAMILY overrides): the
# stock Unsloth UD-Q4_K_XL that gufo and Strata share (scripts/download-stock.sh), and with the saved weights answer
# "abliterated" (LLAMACPP_WEIGHTS overrides) huihui's v2 abliteration as a 2.3 MB rank-1 LoRA on top (--lora,
# scripts/download-abliterated.sh llamacpp) instead of a second ~112 GB model. Not measured here yet: ./harness tune.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
XDG=${XDG_CACHE_HOME:-$HOME/.cache}
GGUF_NAME=${QWEN36_ALT_MODEL:-Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf}
PORT=${PORT:-8080}
CTX=${QWEN36_CTX:-131072}
SPEC=${QWEN36_SPEC:-2}
BATCH=${QWEN36_BATCH:-2048}
VULKAN_IMAGE=ghcr.io/halo-box/strix-llama.cpp@sha256:30370c206723e1c110052e7f1a82cbbf71ca1c7dd865aeca3461df394bc03cc1  # server-vulkan-b13600
ROCM_IMAGE=ghcr.io/halo-box/strix-llama.cpp@sha256:1eaf1f2e7b2a7c02d4b8fd559dd7f65a81fdd6968b8ed08af492cb1559be5bd1    # server-rocm-b13600
# scripts/upgrade-llamacpp.sh stores a validated newer digest per user (harness-pins); a harness update with a newer default wins
. "$HERE/../scripts/lib-pins.sh"
[ "${LLAMACPP_BACKEND:-vulkan}" = rocm ] && IMAGE=${QWEN36_IMAGE:-$(pin_value LLAMACPP_ROCM_IMAGE "$ROCM_IMAGE")} \
  || IMAGE=${QWEN36_IMAGE:-$(pin_value LLAMACPP_VULKAN_IMAGE "$VULKAN_IMAGE")}

find_gguf() {
  [ -n "${QWEN36_GGUF:-}" ] && { echo "$QWEN36_GGUF"; return; }
  local d
  for d in "${QWEN36_DIR:-}" "$HOME/LLMBench/models/qwen36-35b-a3b" "$HOME/models/qwen36-35b-a3b" \
           "$XDG/local-ai-harness/strata-data/Strata-data/models/qwen36-UD-Q4_K_XL"; do
    [ -n "$d" ] && [ -f "$d/$GGUF_NAME" ] && { echo "$d/$GGUF_NAME"; return; }
  done
}
AGENT_DIR=${PI_CODING_AGENT_DIR:-${PI_HOME:-$HOME/.pi}/agent}
FAMILY=${LLAMACPP_FAMILY:-$(cat "$AGENT_DIR/harness-model" 2>/dev/null)}  # no saved answer: Qwen3.6, this script's own model
EXTRA=()
if [ "$FAMILY" = flash-next ]; then
  GGUF=$(GUFO_WEIGHTS=stock GUFO_QUANT=UD-Q4_K_XL bash "$HERE/start-gufo-flashnext.sh" --print-model)  # .../UD-Q4_K_XL/<shard 1>
  ROOT=$(dirname "$(dirname "$GGUF")")
  LORA=$ROOT/HUIHUI-V2-Qwen3.8-Flash-Next-LoRA-F32.gguf
  WEIGHTS=${LLAMACPP_WEIGHTS:-$(cat "$AGENT_DIR/harness-weights" 2>/dev/null)}
  case "${1:-}" in --print-model) echo "$GGUF"; exit 0 ;; --print-lora) echo "$LORA"; exit 0 ;; --print-image) echo "$IMAGE"; exit 0 ;; esac
  # the whole quant, not just shard 1 (it is tiny and lands first)
  N=$(echo "$GGUF" | sed -E 's/.*-of-0*([0-9]+)\.gguf/\1/'); i=1
  while [ "$i" -le "$N" ]; do
    [ -f "${GGUF/-00001-of-/-0000$i-of-}" ] || { echo "stock Flash-Next shard $i of $N missing beside $GGUF - get it: scripts/download-stock.sh" >&2; exit 1; }
    i=$((i + 1))
  done
  if [ "$WEIGHTS" = abliterated ]; then
    [ -f "$LORA" ] || { echo "abliteration LoRA missing: $LORA - get it: scripts/download-abliterated.sh llamacpp   (or run stock: LLAMACPP_WEIGHTS=stock $0)" >&2; exit 1; }
    EXTRA+=(--lora "/models/$(basename "$LORA")")
  fi
  # Flash-Next burns its whole output budget thinking without a server default (server/start-flashnext.sh); pi sends its own
  # shortcut: no vision projector yet (not checked with this image on Flash-Next), add it after ./harness tune
  EXTRA+=(--reasoning-effort medium --reasoning-budget 2048)
  GGUF_NAME=${GGUF#"$ROOT"/}; MOUNT=$ROOT; NAME=qwen3.8-flash-next; CTX=${QWEN36_CTX:-262144}; SPEC_ARGS=()
  # the MTP head scripts/download-stock.sh puts beside the shards (gufo's); v1.1.0 (#304) stopped the long-context load
  # abort with a draft. -lzm on-direct is halo-box's own Flash-Next recipe. FLASHNEXT_MTP=0 turns both off.
  MTP=mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf
  if [ "${FLASHNEXT_MTP:-1}" = 1 ] && [ -f "$ROOT/$MTP" ]; then
    SPEC_ARGS=(-md "/models/$MTP" --spec-type draft-mtp --spec-draft-n-max "${QWEN36_SPEC:-3}" -lzm on-direct)
  fi
else
  GGUF=$(find_gguf); MOUNT=$(dirname "$GGUF"); NAME=${QWEN36_NAME:-qwen36-q4}
  SPEC_ARGS=(--spec-type draft-mtp --spec-draft-n-max "$SPEC")
  [ "${1:-}" = --print-lora ] && exit 0
fi
case "${1:-}" in
  --print-model) echo "$GGUF"; exit 0 ;;
  --print-image) echo "$IMAGE"; exit 0 ;;
esac
[ -f "$GGUF" ] || {  # -f: a QWEN36_GGUF pointing nowhere too
  echo "$GGUF_NAME is not on disk: put it in ~/LLMBench/models/qwen36-35b-a3b/, set QWEN36_GGUF to its path, or" >&2
  echo "let Strata download it (scripts/install-strata.sh) - this launcher reuses that copy. ~22.4 GB." >&2
  exit 1
}
[ "${1:-}" = --check ] && exit 0  # upgrade-llamacpp.sh's preflight: the model files are on disk (each missing one exits 1, named)
# one engine at a time on the shared heap: a Strata (or another llama-server) on the same port must go first
if [ -n "$(docker ps -q --filter "publish=$PORT")" ]; then
  echo "  something already serves :$PORT in docker (stop it first: ./harness config, running; or docker stop <name>)" >&2
  exit 1
fi
RENDER_GID=$(stat -c %g /dev/dri/renderD128 2>/dev/null || echo 44)
VIDEO_GID=$(stat -c %g /dev/dri/card0 2>/dev/null || echo 44)
exec docker run --rm -i --name "llamacpp-$PORT" \
  --device /dev/kfd --device /dev/dri \
  --group-add "${RENDER_GID}" --group-add "${VIDEO_GID}" \
  --security-opt seccomp=unconfined --ipc=host --ulimit memlock=-1:-1 \
  -v "$MOUNT":/models:ro \
  -p "127.0.0.1:$PORT:$PORT" \
  "$IMAGE" \
    -m "/models/$GGUF_NAME" --alias "$NAME" \
    -c "$CTX" -np 1 -fa on -ctk q8_0 -ctv q8_0 \
    "${SPEC_ARGS[@]}" "${EXTRA[@]}" \
    -b "$BATCH" -ub "$BATCH" \
    --host 0.0.0.0 --port "$PORT" --jinja
