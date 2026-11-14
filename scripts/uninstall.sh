#!/usr/bin/env bash
# uninstall.sh — remove everything setup.sh provisioned. Backs up first.
# Leaves alone: your own model entries, non-harness extensions, the halogen
# container/checkpoint, mcp.json, the sidecar GGUF
# (prints the rm command instead).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# decide BEFORE defaulting PI_HOME: an explicit PI_HOME means a sandbox install
SANDBOX=${PI_HOME:+1}
PI_HOME="${PI_HOME:-$HOME/.pi}"
AG="${PI_CODING_AGENT_DIR:-$PI_HOME/agent}"; export AG  # same rule as setup.sh and the wizard
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$HOME/pi-harness-uninstall-backup-$TS.tar.gz"
ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

echo "this removes everything the harness installer provisioned:"
echo "  - extensions, templates, skills, theme under $AG"
echo "  - the providers setup wrote in models.json (halogen, llamacpp-tiny, and the engine you picked: strata, gufo, ...)"
echo "  - pi defaults if they still match what setup set, and the pi packages it installed"
echo "  - ling-tiny systemd unit"
echo "  - NOT touched: mcp.json, the sidecar GGUF, the halogen container,"
echo "    your checkpoint files, the vm-compact.timer (sudo; command printed)"
if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "type 'uninstall' to continue: " A
  [ "$A" = "uninstall" ] || { echo "aborted"; exit 1; }
fi

echo "[1/8] backup -> $BACKUP"
# one tar call: a second `czf` would truncate the first archive (backup lost)
AGREL=$(realpath --relative-to="$PI_HOME" "$AG" 2>/dev/null || echo agent)  # tar members are relative to PI_HOME
TAR_ARGS=(-C "$PI_HOME" "$AGREL/extensions" "$AGREL/prompts" "$AGREL/skills" "$AGREL/themes" "$AGREL/settings.json" "$AGREL/models.json" "$AGREL/mcp.json")
[ -f "$HOME/.config/systemd/user/ling-tiny.service" ] && TAR_ARGS+=(-C "$HOME/.config/systemd/user" ling-tiny.service)
# every state file the harness writes into the agent dir (wizard answers, tuning, markers, logs); one list for backup and removal
STATE=(harness-profile.json harness-tuned.json harness-extras.json harness-sidecar harness-agent harness-update-check harness-update-stamp
       harness-hints-shown harness-repo harness-ubatch harness-events.jsonl setup.log
       harness-weights harness-model harness-quant harness-pick.json harness-pins hud.log progress-stamps.json)
for f in "${STATE[@]}"; do [ -f "$AG/$f" ] && TAR_ARGS+=(-C "$PI_HOME" "$AGREL/$f"); done
[ -f "$AG/rag-index.py" ] && TAR_ARGS+=(-C "$PI_HOME" "$AGREL/rag-index.py") # own -C: tar applies earlier -C to later members
tar czf "$BACKUP" "${TAR_ARGS[@]}" 2>/dev/null
PROFILE_JSON=$(cat "$AG/harness-profile.json" 2>/dev/null); export PROFILE_JSON
echo "      $(du -h "$BACKUP" | cut -f1)"

echo "[2/8] systemd units"
if [ -n "$SANDBOX" ]; then
  echo "      skipped (PI_HOME sandbox - real systemd units untouched)"
else
  systemctl --user disable --now ling-tiny.service >/dev/null 2>&1 && echo "      ling-tiny stopped/disabled"
  rm -f "$HOME/.config/systemd/user/ling-tiny.service"
  if [ -f "$HOME/.config/systemd/user/lemonade-server.service" ]; then
    systemctl --user disable --now lemonade-server.service >/dev/null 2>&1 && echo "      lemonade-server stopped/disabled"
    rm -f "$HOME/.config/systemd/user/lemonade-server.service"
    echo "      note: the lemonade-server package itself is left installed (remove: pacman -R lemonade-server)"
  fi
  if [ -f "$AG/../speak.json" ]; then
    rm -f "$AG/../speak.json" && echo "      speak.json removed (voice config)"
    echo "      privateer-speak package left installed (remove: pi remove npm:privateer-speak)"
  fi
  systemctl --user daemon-reload 2>/dev/null
fi

echo "[3/8] extensions provisioned by the harness"
# whatever setup.sh copies (every extensions/*.ts and extensions/*/), so new extensions are never missed
for p in "$REPO"/extensions/*.ts "$REPO"/extensions/*/; do
  b=$(basename "$p"); [ -e "$AG/extensions/$b" ] && rm -rf "${AG:?}/extensions/$b"
done
[ -d "$AG/progress" ] && rm -rf "${AG:?}/progress"  # progress-tracker checkpoints
for f in "${STATE[@]}"; do rm -f "${AG:?}/$f"; done  # wizard answers, tuning, markers, logs (all in the backup)
[ -d "$AG/extensions/optional" ] && rm -rf "$AG/extensions/optional"
[ -f "$AG/rag-index.py" ] && rm -f "$AG/rag-index.py" && echo "      rag-index.py removed (installed by setup)"
echo "      done (extensions you added yourself are left in place)"

echo "[4/8] templates + skills + theme"
for f in gauntlet.md speedtest.md; do rm -f "$AG/prompts/$f" "$PI_HOME/templates/$f"; done
for d in "$REPO"/skills/*/; do
  b=$(basename "$d"); [ -d "$AG/skills/$b" ] && rm -rf "$AG/skills/$b"
done
# design skills vendored in earlier releases no longer live in skills/ - clean them from
# older installs explicitly (the repo loop above can't see them anymore)
for b in frontend-design token-optimizer ui-ux-pro-max web-design-guidelines; do
  [ -d "$AG/skills/$b" ] && rm -rf "$AG/skills/$b"
done
if [ -f "$AG/themes/opencode.json" ] && cmp -s "$AG/themes/opencode.json" "$REPO/config/themes/opencode.json" 2>/dev/null; then
  rm -f "$AG/themes/opencode.json"; echo "      theme removed (we installed it)"
fi

echo "[5/8] pi defaults + providers"
python3 - <<'PY'
import json, os
home = os.environ["AG"]
# what the wizard chose, saved before step 3 deletes the state files
try:
    prof = json.loads(os.environ.get("PROFILE_JSON") or "{}")
except ValueError:
    prof = {}
ours_prov = {"halogen", "llamacpp-tiny"} | ({prof["provider"]} if prof.get("provider") else set())
sp = os.path.join(home, "settings.json")
if os.path.exists(sp):
    s = json.load(open(sp))
    # defaults: remove only if they still match what setup set
    for k, ours in (("defaultProvider", prof.get("provider") or "halogen"), ("defaultModel", prof.get("model") or "halogen-qwen3.8-flash-next"), ("defaultThinkingLevel", "medium")):
        if s.get(k) == ours: s.pop(k, None)
    ours_pkgs = ("ponytail", "rpiv-ask-user-question", "pi-rtk-optimizer")  # adopt.py PACKAGES
    s["packages"] = [p for p in s.get("packages", []) if not any(o in str(p) for o in ours_pkgs)]
    json.dump(s, open(sp, "w"), indent=2); open(sp, "a").write("\n")
mp = os.path.join(home, "models.json")
if os.path.exists(mp):
    m = json.load(open(mp))
    for prov in ours_prov:  # a provider pointing somewhere else than this box is the user's own
        p = m.get("providers", {}).get(prov) or {}
        if prov in ("halogen", "llamacpp-tiny") or "127.0.0.1" in p.get("baseUrl", ""):
            m["providers"].pop(prov, None)
    json.dump(m, open(mp, "w"), indent=2); open(mp, "a").write("\n")
print("      defaults removed where they matched ours; providers removed: " + ", ".join(sorted(ours_prov)))
PY

echo "[6/8] mcp.json"
if [ -f "$AG/mcp.json" ]; then
  echo "      left in place (contains serena/jina/playwright choices you may want elsewhere)"
fi

[ -f "$AG/AGENTS.md" ] && echo "      AGENTS.md left in place (user-owned)"

echo "[7/8] sidecar GGUF"
GG=$(find "$HOME/.models" "$HOME/Downloads" "$HOME/LLMBench/models" -maxdepth 3 -name "Ling-3.0-tiny-Q4_K_M.gguf" 2>/dev/null | head -1)
if [ -n "$GG" ]; then
  echo "      kept: $GG ($(du -h "$GG" | cut -f1)) - rm it manually to reclaim the space"
else
  echo "      not found (nothing to reclaim)"
fi

echo "[8/8] notes"
echo "      halogen container/checkpoint: untouched (docker stop <id> + rm the model dir if desired)"
echo "      vm-compact.timer (sudo): sudo rm /etc/systemd/system/vm-compact.{service,timer} && sudo systemctl daemon-reload"
echo "      backup: $BACKUP (restore by untarring into ~)"
echo "DONE - the harness is removed (restart any running pi session). pi itself, mcp.json and your own models.json entries are left as they were."
