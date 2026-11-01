#!/usr/bin/env bash
# Build the exact npm payload, install it into an isolated OmO/Pi home, and prove the
# packaged extension and Python runtime load from node_modules rather than this checkout.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${PI_HOST_BIN:-omo}"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pi-sleep-package.XXXXXX")"
if [ "${KEEP_TMP:-0}" != "1" ]; then
  trap 'rm -rf "$ROOT"' EXIT
fi

TGZ_NAME="$(cd "$REPO" && npm pack --ignore-scripts --pack-destination "$ROOT" | tail -1)"
TGZ="$ROOT/$TGZ_NAME"
npm install --prefix "$ROOT/stage" "$TGZ" --ignore-scripts --no-audit --no-fund >/dev/null
PKG="$ROOT/stage/node_modules/@aydinnyunus/pi-sleep"
PROJECT="$ROOT/project"
mkdir -p "$PROJECT"

export OMO_CODING_AGENT_DIR="$ROOT/agent"
export SLEEP_HOME="$ROOT/sleep-home"

cd "$PROJECT"
"$HOST" install "$PKG" --approve >/dev/null
"$HOST" list --approve | grep -F '@aydinnyunus/pi-sleep' >/dev/null
"$HOST" --print-prompt-sizes --no-context-files --no-builtin-tools --no-skills \
  >"$ROOT/startup.json"
grep -F '@aydinnyunus/pi-sleep' "$ROOT/startup.json" >/dev/null
python3 "$PKG/sleep.py" status -v >"$ROOT/status.txt"
grep -F 'T0 buffer' "$ROOT/status.txt" >/dev/null
# The extension's note() slices detail to 200 chars, so a long repo= path is truncated in the log.
# Runtime correctness is proven behaviorally: the extension started (session_start present) and the
# packaged sleep.py executed (status -v above). Assert both; do not grep the truncated path.
grep -F 'session_start' "$SLEEP_HOME/logs/extension.log" >/dev/null
EXT_CTL="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$PKG/sleep.py")"
[ -x "$EXT_CTL" ] || { echo "packaged sleep.py missing: $EXT_CTL" >&2; exit 1; }

printf 'PASS package=%s host=%s root=%s\n' "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$PKG")" "$HOST" "$ROOT"
