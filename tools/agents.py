#!/usr/bin/env python3
"""Registry of coding-agent targets the sleep cycle can capture from.

Each target knows how to locate its hooks config, which events to wire (mapped to
the repo\'s hook scripts), and how to (un)wire them. install.py drives this table
through an interactive user choice - the person installing decides which agents
get the sleep hooks, instead of the installer assuming "claude + pi".

Two capture gradients exist today:
  * shell-hook targets: config is a JSON "hooks" object mapping event -> [{hooks:
    [{type:command,command:<abs script>,timeout}]}], which tools/wire.py can merge.
  * extension targets: a TypeScript extension loaded by the harness (Pi/OmO and
    its forks), linked into a harness extensions dir. Not JSON-hook shaped.

Everything below is data; the merge/backup/remove logic stays in tools/wire.py so
backups, dry-run shaping, and idempotence live in exactly one place.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _home() -> str:
    return os.path.expanduser("~")


def _default(home_path: str, env: str) -> str:
    """Resolve an agent config home: $ENV override, else the default path."""
    return os.environ.get(env) or os.path.expanduser(home_path)


def _script(name: str) -> str:
    return os.path.join(ROOT, "hooks", name)


# ---- shell-hook targets: each is wired via wire.merge() into a JSON hooks file ----
# event -> hook script name (the profile column in wire.WIRING tags these "claude",
# but a registry entry drives which events + which config file a target uses).

# Claude Code (measured): reads settings.json under CLAUDE_HOME (~/.claude-work).
CLAUDE = {
    "key": "claude",
    "label": "Claude Code",
    "kind": "shell-hook",
    "home_env": "CLAUDE_HOME",
    "config": lambda env: os.path.join(env.get("CLAUDE_HOME") or _default("~/.claude-work", "CLAUDE_HOME"), "settings.json"),
    "wiring": {
        "UserPromptSubmit": "sleep-capture.sh",
        "PostToolUse": "sleep-posttool.sh",
        "Stop": "sleep-consolidate.sh",
        "PreCompact": "sleep-consolidate.sh",
    },
    "help": "Hooks into UserPromptSubmit/PostToolUse/Stop/PreCompact in your Claude Code settings.json.",
}

# Pi / OmO (measured): shell hooks never fire; capture runs through a TS extension.
# Extension placed in $OMO_HOME/extensions/sleep. Also clears legacy pi hooks.json
# entries (the engine cannot run them).
PI = {
    "key": "pi",
    "label": "Pi / OmO",
    "kind": "extension",
    "home_env": "OMO_HOME",
    "extension_dir": lambda env: os.path.join(env.get("OMO_HOME") or _default("~/.omo/agent", "OMO_HOME"), "extensions"),
    "hooks_json": lambda env: os.path.join(env.get("OMO_HOME") or _default("~/.omo/agent", "OMO_HOME"), "hooks.json"),
    "help": "Links the TypeScript capture extension into the OmO/Pi extensions dir (loaded on next session).",
}

# ---- Cursor (native hooks.json grammar, cursor.com/docs/hooks) ----
# Wired with the dedicated cursor writer in tools/wire.py: the file is
# {"version":1,"hooks":{event:[{type,command,matcher,timeout}]}} and events are
# camelCase. Cursor exposes no prompt-text event, so capture is implicit: we wire
# postToolUse -> sleep-posttool.sh and sessionEnd -> sleep-consolidate.sh.
CURSOR = {
    "key": "cursor",
    "label": "Cursor",
    "kind": "cursor-hooks",
    "home_env": None,
    "config": lambda env: os.path.join(env.get("CURSOR_HOME") or _default("~/.cursor", "CURSOR_HOME"), "hooks.json"),
    "wiring": {
        "sessionEnd": "sleep-consolidate.sh",
        "postToolUse": "sleep-posttool.sh",
    },
    "help": "Writes postToolUse/sessionEnd entries into ~/.cursor/hooks.json (Cursor-native camelCase grammar).",
}

SHELL_HOOK_TARGETS = [CLAUDE]
EXTENSION_TARGETS = [PI]
CURSOR_TARGETS = [CURSOR]

ALL_TARGETS = SHELL_HOOK_TARGETS + EXTENSION_TARGETS + CURSOR_TARGETS


def by_key(key: str) -> dict | None:
    for t in ALL_TARGETS:
        if t["key"] == key:
            return t
    return None


def known_keys() -> list:
    return [t["key"] for t in ALL_TARGETS]


def target_labels() -> dict:
    return {t["key"]: t["label"] for t in ALL_TARGETS}
