#!/usr/bin/env bash
# Remove the sleep cycle (cross-platform, thin shim).
#
#   ./uninstall.sh                 remove skill, hooks, scheduler, extension data
#   ./uninstall.sh --dry-run       print every action, change nothing
#   ./uninstall.sh --purge         also move ~/.sleep aside to ~/.sleep-pre-purge-<ts>
#
# The real driver is tools/uninstall.py so the same uninstall runs on macOS,
# Linux, and Windows (there: `python tools/uninstall.py`). This shim keeps the
# documented ./uninstall.sh interface working.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "python3 not found; refusing to half-uninstall" >&2
  exit 1
fi
exec "$PY" "$ROOT/tools/uninstall.py" "$@"
