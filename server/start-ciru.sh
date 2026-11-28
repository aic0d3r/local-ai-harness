#!/usr/bin/env bash
# CIRU (ciru-ai/Inkling-2-Mini-CIRU-Halo-Agent-gfx1151): Inkling 2 Mini on vLLM 0.34 ROCm with CIRU's native gfx1151
# HIP kernels and Inco AI's DFlash 2 drafter, on :18753. Strix Halo (gfx1151) only. ~82 GiB of the GPU heap at 1M
# context: one big engine at a time, stop halogen/gufo/osmos first (the wizard does).
#   server/start-ciru.sh             serve (execs CIRU's serve.sh)
#   server/start-ciru.sh --check     exit 1 and list what is missing (runtime, weights, drafter)
#   server/start-ciru.sh --install   fetch and install what is missing (~20 GB venv, runtime, ~23 GB of weights); no sudo
#   server/start-ciru.sh --print-dir the runtime checkout (the update check reads its tag)
# CIRU_TAG picks the release, CIRU_CTX / CIRU_KV_MIB the window and KV budget (defaults: CIRU's recommended DFlash 2 launch).
set -u
ENGINES=$([ -d "$HOME/LLMBench/engines" ] && echo "$HOME/LLMBench/engines" || echo "$HOME/coding")
MODELS=${CIRU_MODELS:-$([ -d "$HOME/LLMBench/models" ] && echo "$HOME/LLMBench/models" || echo "$HOME/models")}
SRC=${CIRU_DIR:-$ENGINES/ciru-inkling}
TAG=${CIRU_TAG:-v1.7.0}
VENV=${CIRU_VENV:-$HOME/.venvs/inkling-halo}
DEPS=$HOME/.local/share/inkling-halo/transformers-deps
PREFIX=$HOME/.local/opt/inkling-halo-$TAG
MODEL=$MODELS/Inkling-2-Mini-CIRU-Halo-Agent-gfx1151
DRAFT=$MODELS/Inkling-2-Mini-DFlash2
PORT=${CIRU_PORT:-18753}
REPO=https://github.com/ciru-ai/Inkling-2-Mini-CIRU-Halo-Agent-gfx1151.git

missing() {  # one path a line; exit 1 when any
  local m=()
  [ -x "$PREFIX/serve.sh" ] || m+=("$PREFIX/serve.sh")
  [ -x "$VENV/bin/python" ] || m+=("$VENV/bin/python")
  [ -s "$MODEL/model-00001-of-00001.safetensors" ] && [ -f "$MODEL/WEIGHTS.sha256" ] || m+=("$MODEL")
  [ -f "$DRAFT/config.json" ] || m+=("$DRAFT")
  [ ${#m[@]} -eq 0 ] || { printf '%s\n' "${m[@]}"; return 1; }
}

case "${1:-}" in
  --print-dir) echo "$SRC"; exit 0 ;;
  --check) missing; exit ;;
  --install)
    set -e
    . "$(dirname "$0")/../scripts/lib-hf.sh"
    [ -d "$SRC/.git" ] || git clone -q "$REPO" "$SRC"
    git -C "$SRC" fetch -q --tags origin && git -C "$SRC" switch -q --detach "$TAG"
    if [ ! -x "$VENV/bin/python" ]; then  # CIRU's INSTALL.md, step 1 (pinned ROCm 10 wheels from vLLM's index)
      uv venv --python 3.12 "$VENV"
      uv pip install --python "$VENV/bin/python" --extra-index-url https://wheels.vllm.ai/rocm/0.34.0/rocm100/ \
        'vllm==0.34.0+rocm100' 'huggingface-hub==2.4.0' 'safetensors==0.8.0'
      uv pip install --python "$VENV/bin/python" --target "$DEPS" --no-deps 'huggingface-hub==1.31.0' 'safetensors==0.8.0'
    fi
    HF_BIN=$(command -v hf || echo "$VENV/bin/hf")
    hf_dl jcbtc/Inkling-2-Mini-CIRU-Halo-Agent-gfx1151 --local-dir "$MODEL"
    hf_dl incoai/Inkling-2-Mini-DFlash2 --local-dir "$DRAFT"
    # install.sh checks the bundle and weight checksums itself
    exec bash "$SRC/scripts/install.sh" --venv "$VENV" --transformers-deps "$DEPS" --prefix "$PREFIX" ;;
  "") ;;
  *) echo "usage: $0 [--check|--install|--print-dir]" >&2; exit 2 ;;
esac

if ! miss=$(missing); then
  echo "ciru: not installed, missing: $(echo "$miss" | tr '\n' ' ')(run $0 --install)" >&2
  exit 1
fi
exec "$PREFIX/serve.sh" --python "$VENV/bin/python" --model "$MODEL" --dflash2 "$DRAFT" --port "$PORT" \
  --served-model-name ciru-inkling-2-mini --context "${CIRU_CTX:-1048576}" --kv-mib "${CIRU_KV_MIB:-49152}" --tools
