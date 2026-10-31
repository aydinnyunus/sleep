#!/usr/bin/env bash
# Wake-phase capture (Layer 2). UserPromptSubmit -> T0 with source=user.
#
# This is the only path that may produce source=user records, because it sees the
# user's own words instead of a model's summary of them. Parsing and every gate
# live in sleepkit/hookio.py; this script exists only because a harness needs a
# file to exec. It must never print to stdout (stdout can be injected into
# context) and must never block, so it exits 0 no matter what happens inside.
set +e
umask 077
[ "${SLEEP_DISABLE:-0}" = "1" ] && exit 0
[ -t 0 ] && exit 0                      # stdin is a terminal => manual test run, no payload
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v python3 >/dev/null 2>&1 || exit 0
PYTHONPATH="$REPO" SLEEP_HOME="${SLEEP_HOME:-$HOME/.sleep}" \
    python3 -m sleepkit.hookio capture >/dev/null 2>&1
exit 0
