#!/usr/bin/env bash
# Install the sleep cycle (cross-platform, thin shim).
#
#   ./install.sh                      skill + hooks in the Claude settings file, with backups
#   ./install.sh --dry-run            print every action, change nothing
#   ./install.sh --nightly            also install the daily scheduler pass (launchd / cron / schtasks)
#   ./install.sh --launchd            alias for --nightly (macOS convention kept)
#   ./install.sh --extension          also link the Pi extension into the auto-discovered dir
#   ./install.sh --skip-tests         don't run the test suite first
#
# The real driver is tools/install.py so the same install runs on macOS, Linux,
# and Windows (where you invoke `python tools/install.py` instead). This shim
# exists so the documented `./install.sh` interface keeps working.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "python3 not found; refusing to half-install" >&2
  exit 1
fi
exec "$PY" "$ROOT/tools/install.py" "$@"
