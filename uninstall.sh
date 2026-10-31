#!/usr/bin/env bash
# Remove every trace of the sleep cycle from the harness config. Data under
# ~/.sleep is NOT deleted (it holds provenance for anything you already promoted);
# pass --purge to move it to ~/.sleep-pre-purge instead of deleting it either.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude-work}"
OMO_HOME="${OMO_HOME:-$HOME/.omo/agent}"
SLEEP_HOME="${SLEEP_HOME:-$HOME/.sleep}"
DRY=0; PURGE=0
for a in "$@"; do case "$a" in --dry-run) DRY=1 ;; --purge) PURGE=1 ;; -h|--help) sed -n '2,6p' "$0"; exit 0 ;; esac; done
run() { if [ "$DRY" = 1 ]; then printf '  would: '; printf '%s ' "$@"; printf '\n'; else "$@"; fi; }
PY="$(command -v python3)"

printf '\n== hooks\n'
cd "$ROOT"
run "$PY" tools/wire.py --settings "$CLAUDE_HOME/settings.json" --profile claude --remove $([ "$DRY" = 1 ] && echo --dry-run)
run "$PY" tools/wire.py --settings "$OMO_HOME/hooks.json"       --profile pi     --remove $([ "$DRY" = 1 ] && echo --dry-run)

printf '\n== skill\n'
[ -L "$CLAUDE_HOME/skills/sleep" ] && run rm "$CLAUDE_HOME/skills/sleep"

if launchctl list 2>/dev/null | grep -q com.aydinnyunus.sleep-nightly; then
  printf '\n== launchd\n'
  run launchctl unload "$HOME/Library/LaunchAgents/com.aydinnyunus.sleep-nightly.plist"
  run rm -f "$HOME/Library/LaunchAgents/com.aydinnyunus.sleep-nightly.plist"
fi

# the extension is a symlink into the repo; a plain copy from an older installer
# version is moved aside instead of deleted
if [ -L "$OMO_HOME/extensions/sleep" ]; then run rm "$OMO_HOME/extensions/sleep"
elif [ -d "$OMO_HOME/extensions/sleep" ]; then run mv "$OMO_HOME/extensions/sleep" "$OMO_HOME/extensions/sleep.removed-$(date +%Y%m%dT%H%M%S)"; fi

if [ "$PURGE" = 1 ]; then
  printf '\n== data\n'
  run mv "$SLEEP_HOME" "$HOME/.sleep-pre-purge-$(date +%Y%m%dT%H%M%S)"
fi
printf '\nuninstalled. promoted memory files were left exactly as they are; git revert in the memory repo is the way back.\n'
