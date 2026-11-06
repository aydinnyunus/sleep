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


def env_file() -> str:
    """Optional KEY=VALUE file carrying whatever a headless call needs to log in.

    A launchd job gets the PATH written into its plist and nothing else, so a token an
    interactive shell would have inherited is simply absent at 03:41. It does not
    belong in that plist either: LaunchAgents files are plain text in the home
    directory. It belongs here, written 0600 by its owner and read by this process.
    """
    return os.path.join(home(), "env")


# --- memory repo resolution ------------------------------------------------
# T2 is sleep's own store, not a host's project memory. The host's own store is
# written under its own rule, and the writer here could not know a store's rule
# ahead of a switch on an unset var in an env file, so the directory it picked was
# always the fallback; it also gave one path a store that named another. T2 therefore
# has one home of its own. It used to be the project memory directory of whichever
# agent cwd was current, and that directory is NOT written to any more: promote
# creates its target if it is missing, so the location must not depend on which
# project a hook happened to run in.

SLEEP_MEMORY_ENV = "SLEEP_MEMORY_DIR"


def memory_dir_home() -> str:
    return os.path.join(home(), "memory")


def memory_dir_env() -> str | None:
    """The one override that is honoured, read from the process environment (and
    adopted from the env file by the CLI before any command runs)."""
    value = (os.environ.get(SLEEP_MEMORY_ENV) or "").strip()
    return os.path.expanduser(value) if value else None


def memory_dir(cwd: str | None = None) -> str:
    """Where T2 lives: the override, else sleep's own home, else what already exists.

    ``cwd`` is kept in the signature because callers still pass it, and it stays
    meaningful only for the last branch: an install that has been running since
    before this change keeps reading the project memory directory its records
    actually sit in, instead of one that reads empty and looks like data loss. A
    directory that has never been written to therefore resolves to sleep's own home
    and not to a path built from the caller's cwd. Callers that must inspect a
    *specific* store keep passing it explicitly (store.* all take memory_dir=...).
    """
    env = memory_dir_env()
    if env:
        return env
    ours = memory_dir_home()
    if os.path.isdir(ours) and any(fn.endswith(".md") for fn in os.listdir(ours)):
        return ours
    legacy = _legacy_project_memory_dir(cwd)
    if legacy:
        return legacy
    return ours


def _legacy_project_memory_dir(cwd: str | None) -> str | None:
    """The pre-0.8.23 location, only when it actually holds records.

    Reads the old project key so records promoted before the move are still found;
    returns None (so the caller keeps sleep's own home) when nothing with a body is
    there.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude-work")
    cands = []
    if cwd:
        cands.append(os.path.join(base, "projects", _project_key(cwd), "memory"))
    cands.append(os.path.join(base, "projects", _project_key(os.getcwd()), "memory"))
    for cand in cands:
        try:
            names = os.listdir(cand)
        except OSError:
            continue
        if any(fn.endswith(".md") and fn != "MEMORY.md" for fn in names):
            return cand
    return None


def _project_key(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", os.path.abspath(cwd)).strip("-")


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
