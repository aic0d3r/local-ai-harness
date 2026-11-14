#!/usr/bin/env bash
# osmos (libosmos) serving the osmos-evo-a pack on osmos-api :8091. Weights: stock, or abliterated = huihui-ai's
# UD-IQ1_M patch as an overlay (0.6 GB, the pack's own files untouched; docs/ENGINES.md#abliterated-models).
# osmos holds ~91 GiB of the GPU heap: one big engine at a time, stop halogen/gufo first (the wizard does).
# The pack is laid out by libosmos's own installer, which is also what fetches, verifies and (un)sets the overlay, so
# this launcher calls it rather than repeating that logic: `osmos_install.py <pack> --serve` builds nothing that is
# already built, downloads only what is missing, and execs osmos-api.
set -u
OSMOS_DIR=${OSMOS_DIR:-$([ -d "$HOME/LLMBench/engines/libosmos" ] && echo "$HOME/LLMBench/engines/libosmos" || echo "$HOME/coding/libosmos")}
OSMOS_MODELS=${OSMOS_MODELS:-$([ -d "$HOME/LLMBench/models" ] && echo "$HOME/LLMBench/models" || echo "$HOME/models")}
PACK=${OSMOS_PACK:-osmos-evo-a}
PORT=${OSMOS_PORT:-8091}
CFG=$OSMOS_MODELS/$PACK/osmos.json
# the wizard's saved answer (stock|abliterated); OSMOS_WEIGHTS overrides it. --print-weights only reports.
WEIGHTS="${OSMOS_WEIGHTS:-$(cat "${PI_CODING_AGENT_DIR:-${PI_HOME:-$HOME/.pi}/agent}/harness-weights" 2>/dev/null)}"
WEIGHTS=${WEIGHTS:-stock}

# the file the engine will load in place of the model's own tensors, or "stock"
print_weights() {
  if [ -f "$CFG" ] && python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); sys.exit(0 if c.get("overlay") else 1)' "$CFG" 2>/dev/null; then
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("overlay",""))' "$CFG"
  else
    echo stock
  fi
}
[ "${1:-}" = --print-weights ] && { print_weights; exit 0; }

case "${1:-$WEIGHTS}" in
  stock|abliterated) W=${1:-$WEIGHTS} ;;
  *) echo "usage: $0 [stock|abliterated] [--print-weights]" >&2; exit 2 ;;
esac

[ -x "$OSMOS_DIR/tools/install/osmos_install.py" ] || { echo "libosmos not found at $OSMOS_DIR (set OSMOS_DIR)" >&2; exit 1; }
[ -f "$CFG" ] || { echo "pack not installed: $CFG (run the installer once: tools/install/osmos_install.py $PACK --models $OSMOS_MODELS --serve)" >&2; exit 1; }

# already on the requested weights, and already laid out: nothing to rewrite
want_overlay=$([ "$W" = abliterated ] && echo yes || echo no)
have_overlay=$([ "$(print_weights)" != stock ] && echo yes || echo no)
if [ "$want_overlay" = "$have_overlay" ]; then
  exec "$OSMOS_DIR/bin/osmos-api" --config "$CFG" --port "$PORT"
fi

echo "osmos: switching to the $W weights (the installer fetches the 0.6 GB patch the first time)" >&2
cd "$OSMOS_DIR" || exit 1
flags=(--models "$OSMOS_MODELS" --serve)
[ "$W" = abliterated ] && flags+=(--abliterated)
# --have the models tree so the disk preflight counts what is already on disk instead of the whole pack
exec python3 tools/install/osmos_install.py "$PACK" "${flags[@]}" --trust-size --have "$OSMOS_MODELS"
