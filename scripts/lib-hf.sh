#!/usr/bin/env bash
# Sourced by the download scripts (needs $HF_BIN). Downloads go over plain HTTP, not Xet: huggingface_hub's Xet path
# does not resume (it rewrites the file from byte 0 on every start, measured: a 46.9 GB shard that died at 94% on an
# expired token, "Cannot send a request, as the client has been closed", restarted at 1 GB), while the HTTP path appends
# to the .incomplete file with a Range request (checked: the partial file grows across restarts). So a dropped
# connection or a re-run costs only what was in flight. HF_HUB_DISABLE_XET=0 turns Xet back on; hf_dl retries 5 times.
# A partial file written by an earlier Xet run may have holes: delete its .incomplete file (under
# <local-dir>/.cache/huggingface/download/) before the first HTTP run.
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
# hf_verify REPO REV DIR [--require NAME...]: size + sha256 against the repo's own file list (scripts/verify-hf.py). Fails only on a
# real mismatch; offline it says so and passes (the download itself already succeeded). HF_VERIFY_HASH=0 = sizes only.
hf_verify() { python3 "$(dirname "${BASH_SOURCE[0]}")/verify-hf.py" "$@"; }
# free_gb PATH: GB free where PATH is (or would be created): df on the nearest existing parent
free_gb() { local p=$1; while [ ! -d "$p" ] && [ "$p" != / ]; do p=$(dirname "$p"); done; df -BG --output=avail "$p" 2>/dev/null | tail -1 | tr -dc '0-9'; }
hf_dl() {
  local n
  for n in 1 2 3 4 5; do
    "$HF_BIN" download "$@" && return 0
    [ "$n" -lt 5 ] && { echo "  download interrupted (attempt $n of 5) - resuming ..." >&2; sleep 5; }
  done
  return 1
}
