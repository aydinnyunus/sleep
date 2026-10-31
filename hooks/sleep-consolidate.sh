#!/usr/bin/env bash
# Sleep trigger (Katman 2). Stop / PreCompact -> policy-gated consolidation.
#
# Returns immediately: the consolidation is a background model call, and a hook
# that waits makes the user pay for it twice. All decisions (is there enough in
# the buffer, was the last sleep long enough ago, is another sleep running) live
# in `sleep.py sleep`, not here. Prints nothing; exit 0 always.
set +e
umask 077
[ "${SLEEP_DISABLE:-0}" = "1" ] && exit 0
SLEEPCTL="${SLEEPCTL:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/sleep.py}"
[ -x "$SLEEPCTL" ] || exit 0
SLEEP_HOME="${SLEEP_HOME:-$HOME/.sleep}"
mkdir -p "$SLEEP_HOME/logs" 2>/dev/null

# Consume stdin so the harness is never left writing into a closed pipe.
cat >/dev/null 2>&1 || true

if [ "${SLEEP_SYNC:-0}" = "1" ]; then
  "$SLEEPCTL" sleep --min-records "${SLEEP_MIN_RECORDS:-3}" \
      --min-interval "${SLEEP_MIN_INTERVAL:-6}" >> "$SLEEP_HOME/logs/hook.log" 2>&1
  exit 0
fi

nohup "$SLEEPCTL" sleep --min-records "${SLEEP_MIN_RECORDS:-3}" \
    --min-interval "${SLEEP_MIN_INTERVAL:-6}" >> "$SLEEP_HOME/logs/hook.log" 2>&1 &
disown 2>/dev/null || true
exit 0
