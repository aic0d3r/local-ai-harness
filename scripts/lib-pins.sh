#!/usr/bin/env bash
# Per-user image pins. scripts/upgrade-halogen.sh and upgrade-gufo.sh store a validated newer version HERE instead of
# editing the tracked launchers, so an engine upgrade never dirties the checkout (a dirty tree blocks harness updates).
# The launchers keep the repo's own reviewed pin as the default; a stored pin only counts while the launcher default is
# still the one it was made on top of (BASE_<name>), so a harness update that ships a newer pin wins.
PINS="${PI_CODING_AGENT_DIR:-${PI_HOME:-$HOME/.pi}/agent}/harness-pins"

_pin_get() { sed -n "s/^$1=//p" "$PINS" 2>/dev/null | head -1; }

# pin_value NAME DEFAULT -> the stored value, or DEFAULT
pin_value() {
  local v; v=$(_pin_get "$1")
  if [ -n "$v" ] && [ "$(_pin_get "BASE_$1")" = "$2" ]; then echo "$v"; else echo "$2"; fi
}

# pin_set NAME DEFAULT VALUE
pin_set() {
  local t; mkdir -p "$(dirname "$PINS")" && t=$(mktemp "$PINS.XXXXXX") || return 1
  { grep -v -e "^$1=" -e "^BASE_$1=" "$PINS" 2>/dev/null; echo "BASE_$1=$2"; echo "$1=$3"; } > "$t" && mv "$t" "$PINS"
}
