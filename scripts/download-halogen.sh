#!/usr/bin/env bash
# Fetch exactly the halogen file set (~124 GB, resumable, then checked against the repo's sizes and sha256) into
# ~/models/halogen-qwen3.8-flash-next, or <HALOGEN_MODELS>/halogen-qwen3.8-flash-next when that is set (e.g. a second disk).
#   scripts/download-halogen.sh [--yes]   asks first unless --yes; exits 1 on abort/failure
#   HALOGEN_MODELS=/big/disk/models scripts/download-halogen.sh   the folder is remembered (harness-models-dir) so the launcher finds it
#   HF_VERIFY_HASH=0 skips the sha256 pass (sizes are still checked); HALOGEN_WEIGHTS_REV pins another weights commit
set -u
. "$(dirname "$0")/lib-hf.sh"
REV="${HALOGEN_WEIGHTS_REV:-c464b5b51c968ff54ea78647a4b2556355b36d2c}"
DL_DIR="${HALOGEN_MODELS:-$HOME/models}/halogen-qwen3.8-flash-next"
AVAIL=$(free_gb "$DL_DIR")
HAVE_GB=$(du -sBG "$DL_DIR" 2>/dev/null | cut -f1 | tr -dc 0-9)  # a resume only needs the part still missing
NEED=$((135 - ${HAVE_GB:-0}))
echo "  required download: ~124 GB into $DL_DIR${AVAIL:+ (free: ${AVAIL} GB)}"
# before the question: nobody should agree to a download that cannot finish
if [ "${AVAIL:-999}" -lt "$NEED" ]; then
  echo "  only ${AVAIL} GB free where that is - free up at least $NEED GB, or put it on a bigger disk: HALOGEN_MODELS=/path/to/models $0" >&2; exit 1
fi
if [ "${1:-}" != --yes ]; then
  read -r -p "  download now? [y/N] " A || { echo; echo cancelled; exit 130; }
  case "$A" in [yY]*) ;; *) echo "  aborted - download the files (see README 'Minimum download') or pass a checkpoint: ./setup.sh --halogen /path/to/checkpoint.hgn" >&2; exit 1 ;; esac
fi
HF_BIN="$(command -v hf || command -v huggingface-cli || true)"
if [ -z "$HF_BIN" ]; then
  echo "  no hf cli found - bootstrapping a venv (~40 MB) ..."
  python3 -m venv "$HOME/.hf-venv" 2>/dev/null && "$HOME/.hf-venv/bin/pip" install -q -U huggingface_hub 2>/dev/null \
    && HF_BIN="$HOME/.hf-venv/bin/hf" || { echo "  could not bootstrap the hf downloader - install huggingface_hub (pipx or a venv) and re-run" >&2; exit 1; }
fi
mkdir -p "$DL_DIR"
# a folder other than ~/models: remember it, the launcher looks there first (uninstall removes the note)
if [ -n "${HALOGEN_MODELS:-}" ]; then
  AGENT="${PI_CODING_AGENT_DIR:-${PI_HOME:-$HOME/.pi}/agent}"; mkdir -p "$AGENT" && echo "$HALOGEN_MODELS" > "$AGENT/harness-models-dir"
fi
echo "  downloading (resumable - re-run if interrupted) ..."
# named files only, never the repo: it carries ~190 GB of optional variants (tests/test_download_guard.py keeps it that way)
hf_dl peonist-ai/halogen-qwen3.8-flash-next \
  qwen38-flash-next-v2.hgn qwen38-flash-next-ngram.hgn qwen38-flash-next-vision.hgn tokenizer/ \
  --revision "$REV" \
  --local-dir "$DL_DIR" || { echo "  download failed/incomplete - re-run to resume" >&2; exit 1; }
echo "  checking the files against the repo (size + sha256; a re-run skips files it already checked) ..."
hf_verify peonist-ai/halogen-qwen3.8-flash-next "$REV" "$DL_DIR" --require qwen38-flash-next-v2.hgn qwen38-flash-next-ngram.hgn qwen38-flash-next-vision.hgn tokenizer/ \
  || { echo "  a downloaded file does not match the repo - delete the file(s) named above and re-run" >&2; exit 1; }
# the NPU models live in four separate repos (they are NOT in the main weights repo); left unpinned on purpose:
# each halogen image release needs the matching device files, and upgrade-halogen.sh refreshes them
for n in decider-0.8b qwen3-embedding-0.6b qwen3-reranker-0.6b qwen3guard-gen-0.6b; do
  # hf skips files it already has, so a half-downloaded folder resumes here
  echo "  downloading NPU model: $n ..."
  hf_dl "peonist-ai/halogen-npu-$n" --local-dir "$DL_DIR/npu/$n" \
    || { echo "  NPU model download failed: $n - re-run to resume" >&2; exit 1; }
done
[ -f "$DL_DIR/qwen38-flash-next-v2.hgn" ] || { echo "  download finished but v2.hgn is missing from $DL_DIR" >&2; exit 1; }
echo "  download complete"
