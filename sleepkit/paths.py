"""Filesystem layout for the sleep pipeline.

Three tiers, mirroring the framework's memory hierarchy:

  T0 buffer      $SLEEP_HOME/buffer/<session>.jsonl   wake-time scratch, disposable
  T1 candidates  $SLEEP_HOME/candidates/<id>.md       distilled, provisional, never a directive
  T2 canonical   the user's memory repo               written ONLY by an explicit promote

$SLEEP_HOME defaults to ~/.sleep, override with env SLEEP_HOME.
"""
from __future__ import annotations

import json
import os
import re
import time

SLEEP_HOME = os.environ.get("SLEEP_HOME") or os.path.expanduser("~/.sleep")

def home() -> str:
    d = SLEEP_HOME
    for sub in ("buffer", "candidates", "reports", "logs", "archive/buffer", "archive/candidates",
                ".quarantine", "replay/runs"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d

def buffer_dir() -> str:
    return os.path.join(home(), "buffer")

def candidates_dir() -> str:
    return os.path.join(home(), "candidates")

def quarantine_dir() -> str:
    return os.path.join(home(), ".quarantine")

def reports_dir() -> str:
    return os.path.join(home(), "reports")

def logs_dir() -> str:
    return os.path.join(home(), "logs")

def archive_dir(tier: str = "buffer") -> str:
    d = os.path.join(home(), "archive", tier)
    os.makedirs(d, exist_ok=True)
    return d

def state_path() -> str:
    return os.path.join(home(), "state.json")


# --- memory repo resolution ------------------------------------------------
# Claude Code convention: project key is the cwd with "/" replaced by "-".
# The user's long-term memory lives in the repo-level memory dir; we only ever
# *append* to its index and never rewrite an existing knowledge block.

CLAUDE_HOME = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude-work")


def memory_dir(cwd: str | None = None) -> str:
    env = os.environ.get("SLEEP_MEMORY_DIR")
    if env:
        return os.path.expanduser(env)
    cwd = cwd or os.getcwd()
    key = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.abspath(cwd)).strip("-")
    cand = os.path.join(CLAUDE_HOME, "projects", key, "memory")
    if os.path.isdir(os.path.dirname(cand)) or os.path.isdir(cand):
        return cand
    # Fall back to the global project memory that actually exists on this box.
    fallback = os.path.join(CLAUDE_HOME, "projects", "-Users-yunus-aydin", "memory")
    return fallback if os.path.isdir(fallback) else cand


def new_id(prefix: str = "s") -> str:
    return "%s%d%s" % (prefix, int(time.time()), os.urandom(2).hex())


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def read_state() -> dict:
    p = state_path()
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_state(patch: dict) -> dict:
    st = read_state()
    st.update(patch)
    st["updated"] = now()
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, sort_keys=True)
    os.replace(tmp, state_path())
    return st
