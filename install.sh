#!/usr/bin/env bash
# Install the sleep cycle: skill (Layer 1), hooks (Layer 2), optional nightly
# job and Pi extension. Nothing here writes into the memory repo; only
# `sleep.py promote` ever does, and only when a human asks for it.
#
#   ./install.sh                      skill + hooks in the Claude settings file, with backups
#   ./install.sh --dry-run            print every action, change nothing
#   ./install.sh --launchd            also install the 03:41 nightly pass
#   ./install.sh --extension          also link the Pi extension into the auto-discovered dir
#   ./install.sh --skip-tests         don't run the test suite first
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude-work}"
OMO_HOME="${OMO_HOME:-$HOME/.omo/agent}"
SLEEP_HOME="${SLEEP_HOME:-$HOME/.sleep}"
DRY=0; LAUNCHD=0; EXT=0; SKIP_TESTS=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;; --launchd) LAUNCHD=1 ;; --extension) EXT=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

run() { if [ "$DRY" = 1 ]; then printf '  would: '; printf '%s ' "$@"; printf '\n'; else "$@"; fi; }
step() { printf '\n== %s\n' "$*"; }
PY="$(command -v python3 || true)"
[ -z "$PY" ] && { echo "python3 not found; refusing to half-install" >&2; exit 1; }

step "preflight"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' \
  || { echo "needs python3 >= 3.9, found $("$PY" -V 2>&1)" >&2; exit 1; }
echo "  $("$PY" -V) at $PY"
[ "$SKIP_TESTS" = 1 ] || (cd "$ROOT" && "$PY" -m unittest discover -s tests 2>&1 | tail -3)

step "state dirs"
run "$PY" -c "import os,sys; os.makedirs(sys.argv[1], exist_ok=True)" "$SLEEP_HOME"

step "Layer 1 - skill"
SKILL_TARGET="$CLAUDE_HOME/skills/sleep"
if [ -e "$SKILL_TARGET" ] && [ ! -L "$SKILL_TARGET" ]; then
  echo "  existing $SKILL_TARGET kept aside"
  run mv "$SKILL_TARGET" "$SKILL_TARGET.bak-$(date +%Y%m%dT%H%M%S)"
fi
run mkdir -p "$CLAUDE_HOME/skills"
run ln -sfn "$ROOT/skill/sleep" "$SKILL_TARGET"
echo "  $SKILL_TARGET -> $ROOT/skill/sleep  (shared skill root: visible to Pi and Claude Code)"

step "Layer 2 - hooks"
cd "$ROOT"
run "$PY" tools/wire.py --settings "$CLAUDE_HOME/settings.json" --profile claude $([ "$DRY" = 1 ] && echo --dry-run)
# The Pi engine runs none of these Claude-shaped events (measured seven ways: README
# Limit 4), so an entry in its hooks.json can never fire. An install from before
# Layer 2b left one there; clear it, so the only wiring that exists is the one that
# actually runs. wire.py backs the file up before it writes anything.
if [ -f "$OMO_HOME/hooks.json" ]; then
  run "$PY" tools/wire.py --settings "$OMO_HOME/hooks.json" --profile all --remove $([ "$DRY" = 1 ] && echo --dry-run)
fi

if [ "$LAUNCHD" = 1 ]; then
  step "nightly pass (03:41)"
  PLIST_SRC="$ROOT/launchd/com.aydinnyunus.sleep-nightly.plist"
  PLIST_DST="$HOME/Library/LaunchAgents/com.aydinnyunus.sleep-nightly.plist"
  if [ "$DRY" = 1 ]; then
    echo "  would: render $PLIST_SRC -> $PLIST_DST and load it"
  else
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s#__SLEEPCTL__#$ROOT/sleep.py#" -e "s#__SLEEPHOME__#$SLEEP_HOME#" \
        -e "s#/usr/bin/python3#$PY#" "$PLIST_SRC" > "$PLIST_DST"
    # launchctl registers in the LOGGED-IN user's domain, which $HOME redirection does
    # not move. A sandboxed install (HOME=/tmp/something) would otherwise load a nightly
    # job into the real session. Measured: a sandbox --launchd run did exactly that, and
    # the job was live until booted out. So only load when HOME is the real home.
    REAL_HOME="$(dscl . -read /Users/"$(id -un)" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
    [ -n "$REAL_HOME" ] || REAL_HOME="$(eval echo ~"$(id -un)")"
    if [ "$HOME" != "$REAL_HOME" ]; then
      echo "  rendered $PLIST_DST, NOT loaded: HOME ($HOME) is not the real home ($REAL_HOME)."
      echo "  launchctl would register this in your live session. To load it for real:"
      echo "    launchctl load \"$PLIST_DST\""
    else
      launchctl unload "$PLIST_DST" 2>/dev/null
      launchctl load "$PLIST_DST" && echo "  loaded $PLIST_DST"
    fi
  fi
fi

if [ "$EXT" = 1 ]; then
  step "Pi extension (opt-in)"
  # $OMO_HOME/extensions/ is an auto-discovered location on this engine - measured,
  # not assumed: a symlink placed there loads, session_start fires, and provisional
  # memory reaches the session, all without touching settings.json. A bad entry in
  # "packages" would break every session, so that file stays out of this.
  DEST="$OMO_HOME/extensions/sleep"
  if [ -L "$DEST" ]; then
    run rm "$DEST"
  elif [ -d "$DEST" ]; then
    run mv "$DEST" "$DEST.bak-$(date +%Y%m%dT%H%M%S)-sleep"
  fi
  run mkdir -p "$OMO_HOME/extensions"
  run ln -sfn "$ROOT/extension" "$DEST"
  if [ "$DRY" = 0 ] && [ ! -f "$DEST/index.ts" ]; then
    echo "  linked but index.ts does not resolve - aborting" >&2; exit 1
  fi
  cat <<NOTE
  Placed in the auto-discovered dir; it loads on the next session. Prove it:
    cd "$ROOT" && omo -p 'hi' >/dev/null 2>&1; tail -4 "$SLEEP_HOME/logs/extension.log"
  Expect session_start / publish / agent_settled lines there - that trace exists because
  "no record" and "never loaded" were otherwise indistinguishable. Kill switch:
  SLEEP_DISABLE=1. Uninstall: ./uninstall.sh (removes the link only).
NOTE
fi

step "done"
cat <<NOTE
  Next:
    $ROOT/sleep.py status -v
    $ROOT/sleep.py sleep --dry-run
    $ROOT/sleep.py replay --limit 3          # dry-run plan; --run costs model budget
  Revert everything: $ROOT/uninstall.sh
NOTE
