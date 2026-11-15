#!/usr/bin/env bash
# Sourced by setup.sh and the download/uninstall scripts: every shell question looks and cancels like the wizard's.
# prompt_yn QUESTION [y|n] [DETAIL] -> 0 yes, 1 no. Enter takes the default. Esc + Enter, Ctrl-C or Ctrl-D cancels (exit 130).
# DETAIL is printed on its own line above the prompt, so a long question stays readable.
# Without a terminal nobody can answer: abort (exit 1), as the download scripts always did.
prompt_yn() {
  local a def=${2:-n} hint='[y/N]'
  [ "$def" = y ] && hint='[Y/n]'
  if [ ! -t 0 ]; then echo "  no terminal to answer '$1': aborted (run it in a terminal, or pass --yes where offered)" >&2; exit 1; fi
  [ -n "${3:-}" ] && echo "  $3"
  trap 'echo; echo cancelled; exit 130' INT
  read -r -p "  $1 $hint (Esc + Enter cancels) " a || { trap - INT; echo; echo cancelled; exit 130; }
  trap - INT
  case "$a" in *$'\033'*) echo cancelled; exit 130 ;; esac
  case "${a:-$def}" in [yY]*) return 0 ;; *) return 1 ;; esac
}
