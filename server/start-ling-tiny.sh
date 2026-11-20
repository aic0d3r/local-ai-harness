#!/usr/bin/env bash
# Ling-3.0-tiny aux server (compaction/triage/repomap summaries) on :8090.
# Chat endpoint + template are mandatory for this model (raw /completion degenerates); the extensions use chat.
# Engine: the same pinned halo-box/strix-llama.cpp image as server/start-qwen36.sh (has bailingmoe3, Mesa bundled),
# so nothing to build. LLAMA_SERVER=<binary> (or ENGINE_DIR=<dir holding llama-server>) runs a native build instead.
#   LING_GGUF (default $MODEL_DIR or ~/.models/ling3.0-tiny, Ling-3.0-tiny-Q4_K_M.gguf), UBBATCH, LING_IMAGE
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
GGUF=${LING_GGUF:-${MODEL_DIR:-$HOME/.models/ling3.0-tiny}/Ling-3.0-tiny-Q4_K_M.gguf}
[ -f "$GGUF" ] || { echo "$GGUF not found (setup.sh downloads it; or set LING_GGUF)" >&2; exit 1; }
# ubatch knob shared with /tune (harness-ubatch). Default 4096: the measured
# 2048 deep-fill winner is from 27B decode benches, never measured on tiny;
# 4096 covers typical summarization payloads in one prefill batch.
UBBATCH=${UBBATCH:-$(cat "${PI_CODING_AGENT_DIR:-${PI_HOME:-$HOME/.pi}/agent}/harness-ubatch" 2>/dev/null || echo 4096)}
PORT=8090
ARGS=(-a ling3.0-tiny -ngl all -fa on -c 131072 -np 1 -b $UBBATCH -ub $UBBATCH -t 16 -tb 32 --jinja
      --metrics --sleep-idle-seconds 600)
LLS=${LLAMA_SERVER:-${ENGINE_DIR:+$ENGINE_DIR/llama-server}}
[ -n "$LLS" ] && exec "$LLS" -m "$GGUF" "${ARGS[@]}" --host 127.0.0.1 --port $PORT
IMAGE=${LING_IMAGE:-$(bash "$HERE/start-qwen36.sh" --print-image)}
docker rm -f ling-tiny >/dev/null 2>&1 || true  # a container left by a killed client would hold the port
exec docker run --rm -i --name ling-tiny \
  --device /dev/kfd --device /dev/dri \
  --group-add "$(stat -c %g /dev/dri/renderD128 2>/dev/null || echo 44)" \
  --group-add "$(stat -c %g /dev/dri/card0 2>/dev/null || echo 44)" \
  --security-opt seccomp=unconfined --ipc=host --ulimit memlock=-1:-1 \
  -v "$(dirname "$GGUF")":/models:ro -p 127.0.0.1:$PORT:$PORT \
  "$IMAGE" -m "/models/$(basename "$GGUF")" "${ARGS[@]}" --host 0.0.0.0 --port $PORT
