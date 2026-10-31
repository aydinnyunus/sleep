#!/usr/bin/env bash
# PostToolUse trace + sleep trigger for the Pi/senpi surface (Layer 2).
#
# Why not UserPromptSubmit here: PostToolUse is the only event verified to fire in
# this harness (~/.omo/agent/hooks.json ships a live PostToolUse entry), and this
# script's own sleep pass is policy-gated, so 99% of invocations are one stat and
# one JSON read with no model call. Tool events carry agent provenance, never
# user provenance - see sleepkit/hookio.capture_tool_from_stdin.
set +e
umask 077
[ "${SLEEP_DISABLE:-0}" = "1" ] && exit 0
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v python3 >/dev/null 2>&1 || exit 0
SLEEP_HOME="${SLEEP_HOME:-$HOME/.sleep}"
[ -d "$SLEEP_HOME" ] || exit 0            # not installed here; stay out of the way

payload="$(cat 2>/dev/null)"
[ -z "$payload" ] && exit 0

printf '%s' "$payload" | PYTHONPATH="$REPO" SLEEP_HOME="$SLEEP_HOME" \
    python3 -m sleepkit.hookio capture-tool >/dev/null 2>&1

# Only wake the consolidation pass when this session actually edited something,
# and never inline: it is a model call.
case "$payload" in
  *'"tool_name":"Write"'*|*'"tool_name":"Edit"'*|*'"tool_name":"MultiEdit"'*)
    mkdir -p "$SLEEP_HOME/logs" 2>/dev/null
    nohup "$REPO/sleep.py" sleep --min-records "${SLEEP_MIN_RECORDS:-3}" \
        --min-interval "${SLEEP_MIN_INTERVAL:-6}" >> "$SLEEP_HOME/logs/hook.log" 2>&1 &
    disown 2>/dev/null || true
    ;;
esac
exit 0
