#!/usr/bin/env bash
# Fetch what gufo needs for Qwen3.8 27B: Unsloth's UD-Q4_K_XL (16.35 GiB) or UD-Q8_K_XL (29.30 GiB) GGUF, the vision
# projector (0.87 GiB) and the DFlash2 draft (1.06 GiB), at the revisions gufo's docs/models page names (the sha256s match
# gufo's model-identities.json). Resumable; asks first unless --yes; exits 1 on abort/failure. Not gated.
#   scripts/download-qwen38-27b.sh [--yes]     folder: GUFO_27B_DIR, else <ENGINE_DIR's parent>/qwen38-27b
#   scripts/download-qwen38-27b.sh --print-gb  GB still to fetch (0 = complete); the wizard's question reads it
# quant: GUFO_FAMILY (qwen38-27b = Q4, qwen38-27b-q8 = Q8), else the saved model answer when it is a 27B, else Q4
# Q8 with the saved weights answer abliterated (or GUFO_WEIGHTS=abliterated): huihui-ai's abliterated UD-Q8_K_XL as the
# target (pinned below; projector and draft stay Unsloth's and z-lab's: abliteration changes only the language weights)
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
. "$REPO/scripts/lib-hf.sh"
. "$REPO/scripts/lib-prompt.sh"
FAM="${GUFO_FAMILY:-$(cat "${PI_CODING_AGENT_DIR:-${PI_HOME:-$HOME/.pi}/agent}/harness-model" 2>/dev/null)}"
case "$FAM" in qwen38-27b|qwen38-27b-q8) ;; *) FAM=qwen38-27b ;; esac
FIRST=$(GUFO_FAMILY=$FAM bash "$REPO/server/start-gufo-flashnext.sh" --print-model)
DIR=$(dirname "$FIRST"); TARGET=$(basename "$FIRST"); DRAFT=Qwen3.8-27B-DFlash2-Q4_K_M.gguf
REV_TARGET=4ca720788d1e01f1bff70c033e0d0028fd02e502; REV_DRAFT=2d9571f8ce46e151f61c6499c99dee6079e1d610
UNSLOTH=unsloth/Qwen3.8-27B-GGUF; TREPO=$UNSLOTH; TREV=$REV_TARGET
# a third-party repo: pinned to the commit current on 2026-11-16, a later push must not change what runs
case "$TARGET" in Huihui-*) TREPO=huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF; TREV=c4b90228246aef7ecc6cb2be9d80575a911a0f3a ;; esac
# only what is missing: Q4 and Q8 share the draft and projector, so a second quant fetches just its target
# sizes in GB (10^9) at the pinned revisions, rounded up
size_gb() { case "$1" in *Q8_K_XL*) echo 32 ;; *Q4_K_XL*) echo 18 ;; *) echo 1 ;; esac; }
NEED=(); GB=0
for f in "$TARGET" mmproj-BF16.gguf "$DRAFT"; do
  [ -f "$DIR/$f" ] || { NEED+=("$f"); GB=$((GB + $(size_gb "$f"))); }
done
[ "${1:-}" = --print-gb ] && { echo "$GB"; exit 0; }
[ "$GB" = 0 ] && { echo "  already there: $DIR"; exit 0; }
mkdir -p "$DIR"
AVAIL=$(df -BG --output=avail "$DIR" 2>/dev/null | tail -1 | tr -dc '0-9')
UP=(); DL=(); TG=()  # files per repo: Unsloth (projector), z-lab (draft), the target's
for f in "${NEED[@]}"; do case $f in "$DRAFT") DL+=("$f") ;; "$TARGET") TG+=("$f") ;; *) UP+=("$f") ;; esac; done
Q=${TARGET#*Qwen3.8-27B-}; echo "  Qwen3.8 27B ${Q%.gguf}: ~$GB GB to fetch${AVAIL:+, ${AVAIL} GB free}"
echo "    into  $DIR"
[ ${#TG[@]} -gt 0 ] && echo "    from  $TREPO @ ${TREV:0:8}: ${TG[*]}"
[ ${#UP[@]} -gt 0 ] && echo "    from  $UNSLOTH @ ${REV_TARGET:0:8}: ${UP[*]}"
[ ${#DL[@]} -gt 0 ] && echo "    from  z-lab/Qwen3.8-27B-DFlash2-GGUF @ ${REV_DRAFT:0:8}: ${DL[*]}"
HAVE=$(for f in "$TARGET" mmproj-BF16.gguf "$DRAFT"; do [ -f "$DIR/$f" ] && case $f in mmproj*) printf 'vision projector, ' ;; *DFlash2*) printf 'DFlash2 draft, ' ;; *) printf 'target, ' ;; esac; done)
[ -n "$HAVE" ] && echo "    kept  ${HAVE%, } (already here)"
if [ "${1:-}" != --yes ]; then
  prompt_yn "download now?" n || { echo "  aborted" >&2; exit 1; }
fi
[ "${AVAIL:-999}" -lt $((GB + 5)) ] && { echo "  only ${AVAIL} GB free - free up at least $((GB + 5)) GB first" >&2; exit 1; }
HF_BIN="$(command -v hf || command -v huggingface-cli || ls "$HOME/.hf-venv/bin/hf" 2>/dev/null || true)"
if [ -z "$HF_BIN" ]; then
  echo "  no hf cli found - bootstrapping a venv (~40 MB) ..."
  python3 -m venv "$HOME/.hf-venv" 2>/dev/null && "$HOME/.hf-venv/bin/pip" install -q -U huggingface_hub 2>/dev/null \
    && HF_BIN="$HOME/.hf-venv/bin/hf" || { echo "  could not bootstrap the hf downloader - install huggingface_hub and re-run" >&2; exit 1; }
fi
echo "  downloading - resumable: if it stops, re-run and it continues where it left off"
{ [ ${#TG[@]} -eq 0 ] || hf_dl "$TREPO" "${TG[@]}" --revision "$TREV" --local-dir "$DIR"; } \
  && { [ ${#UP[@]} -eq 0 ] || hf_dl "$UNSLOTH" "${UP[@]}" --revision "$REV_TARGET" --local-dir "$DIR"; } \
  && { [ ${#DL[@]} -eq 0 ] || hf_dl z-lab/Qwen3.8-27B-DFlash2-GGUF "${DL[@]}" --revision "$REV_DRAFT" --local-dir "$DIR"; } \
  || { echo "  download stopped before it finished - re-run to resume" >&2; exit 1; }
for f in "$TARGET" "$DRAFT" mmproj-BF16.gguf; do
  [ -f "$DIR/$f" ] || { echo "  download finished but $f is missing from $DIR" >&2; exit 1; }
done
hf_verify "$TREPO" "$TREV" "$DIR" --require "$TARGET" \
  && hf_verify "$UNSLOTH" "$REV_TARGET" "$DIR" --require mmproj-BF16.gguf \
  && hf_verify z-lab/Qwen3.8-27B-DFlash2-GGUF "$REV_DRAFT" "$DIR" --require "$DRAFT" \
  || { echo "  a downloaded file does not match the repo - delete the file(s) named above and re-run" >&2; exit 1; }
echo "  download complete ($DIR)"
