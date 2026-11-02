#!/usr/bin/env python3
"""Registry of coding-agent targets the sleep cycle can capture from.

Each target knows how to locate its hooks config, which events to wire (mapped to
the repo\'s hook scripts), and how to (un)wire them. install.py drives this table
through an interactive user choice - the person installing decides which agents
get the sleep hooks, instead of the installer assuming "claude + pi".

Targets use the native surface each agent exposes: Claude-format hooks, dedicated
Cursor/Windsurf/Cline JSON grammars, Codex TOML notify, OpenCode project plugins,
or a TypeScript extension linked into Pi/OmO-family extension directories.

Everything below is data; the merge/backup/remove logic stays in tools/wire.py so
backups, dry-run shaping, and idempotence live in exactly one place.
"""
from __future__ import annotations

import os
import sys

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

# OpenCode (opencode.ai): TS plugin system. Verified against a real installed plugin
# (ecc-hooks.ts): .opencode/opencode.json registers a plugin dir with "plugin":["./..."],
# and the plugin default-exports async (input) => ({event: handler}). Events used:
# tool.execute.after (posttool), session.idle (consolidate). The plugin file is
# repo's extension/opencode.ts; the installer writes it into the project's
# .opencode/plugins/ dir and points opencode.json at it. No prompt-text event exists,
# so capture is implicit, like Cursor.
OPENCODE = {
    "key": "opencode",
    "label": "OpenCode",
    "kind": "opencode-plugin",
    "home_env": None,
    "config": lambda env: os.path.join(env.get("OPENCODE_HOME") or _default("~/.config/opencode", "OPENCODE_HOME"), "opencode.json"),
    "wiring": {
        "tool.execute.after": "sleep-posttool.sh",
        "session.idle": "sleep-consolidate.sh",
    },
    "plugin_src": os.path.join(ROOT, "extension", "opencode.ts"),
    "plugin_dest": lambda project: os.path.join(project, ".opencode", "plugins", "sleep-opencode.ts"),
    "help": "Writes the sleep OpenCode TS plugin into the active project's .opencode/plugins and registers it in opencode.json.",
}

# Oh My Pi (can1357/oh-my-pi): a fork/rebrand of the pi agent. Same ExtensionAPI
# (pi.on / pi.registerCommand) carries over; only the config home differs, .omp
# instead of .omo/agent. The same TypeScript capture extension is linked. Confidence:
# MEDIUM - fork source was not present locally to verify spellings; event names are
# the canonical pi ones (tool_call/tool_result) which this repo has measured differ
# from OmO's. Best-effort by user directive: a wrong event-name spelling surfaces as
# an empty extension.log, which is user feedback.
OH_MY_PI = {
    "key": "oh-my-pi",
    "label": "Oh My Pi",
    "kind": "extension",
    "home_env": "OMP_HOME",
    "extension_dir": lambda env: os.path.join(env.get("OMP_HOME") or _default("~/.omp/agent", "OMP_HOME"), "extensions"),
    "hooks_json": lambda env: os.path.join(env.get("OMP_HOME") or _default("~/.omp/agent", "OMP_HOME"), "hooks.json"),
    "help": "Links the TypeScript capture extension into Oh My Pi's .omp extensions dir (best-effort).",
}

# Gemini CLI (measured, HIGH): the live ~/.gemini/settings.json on this machine has a
# real "hooks" object in the SAME Claude-format grammar {event:[{matcher,hooks:[{type,
# command,timeout}]}]}. The rtk-hook-gemini.sh runs on BeforeTool today; the Gemini hook
# catalogue includes BeforePrompt (prompt/submit), BeforeTool/AfterTool (tool boundary),
# and a session-end hook. We wire BeforePrompt -> capture, AfterTool -> posttool, and the
# session-end hook -> consolidate. Config lives at $GEMINI_HOME (~/.gemini)/settings.json.
# Risk: Gemini ignores the whole file if malformed, and merge must preserve theme/auth keys.
GEMINI = {
    "key": "gemini",
    "label": "Gemini CLI",
    "kind": "shell-hook",
    "home_env": "GEMINI_HOME",
    "config": lambda env: os.path.join(env.get("GEMINI_HOME") or _default("~/.gemini", "GEMINI_HOME"), "settings.json"),
    "wiring": {
        "BeforePrompt": "sleep-capture.sh",
        "AfterTool": "sleep-posttool.sh",
        "SessionEnd": "sleep-consolidate.sh",
    },
    "help": "Wires BeforePrompt/AfterTool/SessionEnd hooks into ~/.gemini/settings.json (Claude-format hooks object, measured HIGH).",
}

# Windsurf (measured, HIGH): the live ~/.codeium/windsurf/hooks.json has a "hooks" object
# mapping snake_case events -> [{command: <abs shell string>}] (no matcher, no timeout
# field in the measured grammar). post_write_code fires after code is written, and
# pre_user_prompt before a user prompt is sent (the capture boundary). A session-end
# event exists in the same grammar. Because this grammar differs from Claude's (bare
# {command} entries, no wrapper "hooks"/"type"), it cannot reuse wire.merge's entry shape;
# it needs a dedicated windsurf writer (like cursor has one).
WINDSURF = {
    "key": "windsurf",
    "label": "Windsurf",
    "kind": "windsurf-hooks",
    "home_env": "WINDSURF_HOME",
    "config": lambda env: os.path.join(env.get("WINDSURF_HOME") or _default("~/.codeium/windsurf", "WINDSURF_HOME"), "hooks.json"),
    "wiring": {
        "pre_user_prompt": "sleep-capture.sh",
        "post_write_code": "sleep-posttool.sh",
    },
    "help": "Writes pre_user_prompt/post_write_code entries into ~/.codeium/windsurf/hooks.json (measured HIGH).",
}

# Codex CLI (MEDIUM, docs+reference config): ~/.codex/config.toml. There is NO per-turn
# hook; the only on-event mechanism is [notify] { command, on_complete=true } which runs
# a one-shot shell command when a run finishes. That cannot drive per-turn capture, so this
# is a best-effort wiring that fires the consolidate on completion. It is a TOML file, not
# JSON, so it needs its own writer (the [notify] section is a plain table).
CODEX = {
    "key": "codex",
    "label": "Codex CLI",
    "kind": "codex-notify",
    "home_env": "CODEX_HOME",
    "config": lambda env: os.path.join(env.get("CODEX_HOME") or _default("~/.codex", "CODEX_HOME"), "config.toml"),
    "wiring": {
        "on_complete": "sleep-consolidate.sh",
    },
    "help": "Adds a [notify] block to ~/.codex/config.toml that runs the consolidator on run completion (one-shot, not per-turn capture).",
}

def _cline_config(env: dict, platform: str | None = None) -> str:
    """Return the VS Code user settings path Cline reads on each desktop OS."""
    explicit = env.get("CLINE_HOME")
    if explicit:
        return os.path.join(explicit, "settings.json")
    platform = platform or sys.platform
    home = env.get("HOME") or os.path.expanduser("~")
    if platform == "darwin":
        base = os.path.join(home, "Library", "Application Support", "Code", "User")
    elif platform in ("win32", "cygwin"):
        appdata = env.get("APPDATA") or os.path.join(env.get("USERPROFILE") or home,
                                                     "AppData", "Roaming")
        base = os.path.join(appdata, "Code", "User")
    else:
        base = os.path.join(env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                            "Code", "User")
    return os.path.join(base, "settings.json")


# Cline (VS Code extension, LOW): settings live in the VS Code User settings JSON
# (platform-specific VS Code User settings path). Cline exposes cline.execute.actions: an event map
# where keys like blocked / request_created / web_view_updated map to {command}. Only
# request_created approximates prompt-submit; there is no session-end event. Best-effort;
# wiring it is low-confidence and the key must live among the VS Code settings, not the
# extension's globalStorage.
CLINE = {
    "key": "cline",
    "label": "Cline",
    "kind": "cline-actions",
    "home_env": "CLINE_HOME",
    "config": _cline_config,
    "wiring": {
        "request_created": "sleep-capture.sh",
    },
    "help": "Adds a cline.execute.actions entry to VS Code user settings so Cline runs capture on request_created (low confidence).",
}

SHELL_HOOK_TARGETS = [CLAUDE, GEMINI]
EXTENSION_TARGETS = [PI, OH_MY_PI]
CURSOR_TARGETS = [CURSOR]
OPENCODE_TARGETS = [OPENCODE]
WINDSURF_TARGETS = [WINDSURF]
CODEX_TARGETS = [CODEX]
CLINE_TARGETS = [CLINE]

ALL_TARGETS = (SHELL_HOOK_TARGETS + EXTENSION_TARGETS + CURSOR_TARGETS + OPENCODE_TARGETS
               + WINDSURF_TARGETS + CODEX_TARGETS + CLINE_TARGETS)


def by_key(key: str) -> dict | None:
    for t in ALL_TARGETS:
        if t["key"] == key:
            return t
    return None


def known_keys() -> list:
    return [t["key"] for t in ALL_TARGETS]


def target_labels() -> dict:
    return {t["key"]: t["label"] for t in ALL_TARGETS}
