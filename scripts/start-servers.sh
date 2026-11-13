#!/usr/bin/env bash
# start the engine ./setup.sh picked (halogen, gufo or Strata) + the ling-tiny sidecar headless (no terminal,
# no pi) and notify when ready. Same step start.sh runs before it opens pi (./harness up); log: ~/.cache/local-ai-harness/start-servers.log.
# Without a terminal nothing is asked: if another engine holds the GPU memory, it is left running and this says so.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENG=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["engine"])' \
  "${PI_CODING_AGENT_DIR:-${PI_HOME:-$HOME/.pi}/agent}/harness-profile.json" 2>/dev/null || echo halogen)
notify-send -i media-playback-start "LLM servers" "Starting $ENG + ling-tiny (up to ~5 min)..." 2>/dev/null
LOG="${XDG_CACHE_HOME:-$HOME/.cache}/local-ai-harness/start-servers.log"; mkdir -p "$(dirname "$LOG")"  # not /tmp: a fixed name there can be a planted symlink
python3 -u "$REPO/setup/harness.py" up < /dev/null > "$LOG" 2>&1
if python3 "$REPO/setup/harness.py" status | grep -q ' UP '; then
  msg="$ENG is up. Run pi (or ./start.sh <project>)."
else
  msg="$ENG did not start: $(grep -E '^!|failed|FAIL' "$LOG" | tail -1 | cut -c1-150) (see $LOG)"
fi
echo "$msg"; notify-send -i dialog-information "LLM servers" "$msg" 2>/dev/null
