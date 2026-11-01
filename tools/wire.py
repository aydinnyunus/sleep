#!/usr/bin/env python3
"""Add or remove the sleep hooks in a Claude-format settings/hooks file.

Only touches the events it knows, only inside the ``hooks`` object, and refuses to
guess: if an event's existing shape is not ``[{matcher?, hooks:[{type,command}]}]``
the file is reported and left alone rather than reshaped. Backups are timestamped
next to the original, and ``--dry-run`` prints what would change.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil

# event -> (hook script, profile). Measured on this machine with Claude Code
# 2.1.294 and a sentinel hook: UserPromptSubmit, PostToolUse and Stop all fire and
# their payloads carry what the scripts need (tool_name, tool_input.file_path).
# The Pi/omo engine runs none of these (see README, Limit 4), so no event is wired
# into its hooks.json any more: Layer 2b covers capture there. An entry left in
# ~/.omo/agent/hooks.json is dead config, so install removes an old one.
WIRING = {
    "UserPromptSubmit": ("sleep-capture.sh", "claude"),
    "Stop": ("sleep-consolidate.sh", "claude"),
    "PreCompact": ("sleep-consolidate.sh", "claude"),
    "PostToolUse": ("sleep-posttool.sh", "claude"),
}


def entry(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command, "timeout": 30}]}


def shape_ok(entries) -> bool:
    return isinstance(entries, list) and all(
        isinstance(e, dict) and isinstance(e.get("hooks"), list) for e in entries
    )


# ---- Cursor native hooks grammar (cursor.com/docs/hooks) ----
# ~/.cursor/hooks.json is {"version":1,"hooks":{event:[{type,command,matcher,timeout}]}}
# with camelCase event names. Different from the Claude settings.json shape wire.py
# merges above (no wrapper "hooks" array per entry), so Cursor keeps its own writer
# instead of forcing the Claude shape onto it. Cursor exposes no prompt-text event,
# so capture is implicit: we wire postToolUse to the posttool capture script and
# sessionEnd to consolidation. matcher defaults to * (match everything).
CURSOR_EVENTS = ["sessionStart", "sessionEnd", "afterFileEdit", "postToolUse", "beforeShellExecution"]


def _cursor_entry(command: str, timeout: int = 30) -> dict:
    return {"type": "command", "command": command, "matcher": ".*", "timeout": timeout}


def cursor_entries(events, command: str, timeout: int = 30) -> dict:
    return {ev: [_cursor_entry(command, timeout)] for ev in events}


def wire_cursor(doc: dict, root: str, remove: bool, events=("sessionEnd", "postToolUse"), timeout: int = 30):
    """Add/remove the sleep scripts in a Cursor-format hooks doc (version:1 grammar).

    Refuses to reshape a version it does not recognize, mirroring wire.merge()'
    safety posture for the Claude shape. ``doc`` is mutated in place and the report
    lines returned (backup/write is the caller's job, as for the claude merge).
    """
    report = []
    ver = doc.get("version", 1)
    if not isinstance(doc.get("hooks"), dict):
        report.append("hooks: missing hooks object, left alone")
        return report
    hooks = doc["hooks"]
    for ev in events:
        command = os.path.join(root, "hooks", "sleep-posttool.sh" if ev == "postToolUse" else "sleep-consolidate.sh")
        bucket = hooks.get(ev, [])
        # Cursor entries are bare dicts without a "hooks" wrapper; refuse anything else
        if bucket and not (isinstance(bucket, list) and all(isinstance(e, dict) and "command" in e for e in bucket)):
            report.append("%s: unrecognized shape, left alone" % ev)
            continue
        mine = [e for e in bucket if isinstance(e, dict) and command in str(e.get("command", ""))]
        if remove:
            rest = [e for e in bucket if e not in mine]
            if mine:
                report.append("%s: removed %s" % (ev, os.path.basename(command)))
            if rest:
                hooks[ev] = rest
            else:
                hooks.pop(ev, None)
            continue
        if mine:
            report.append("%s: already wired, unchanged" % ev)
            continue
        hooks.setdefault(ev, []).append(_cursor_entry(command, timeout))
        report.append("%s: added %s" % (ev, os.path.basename(command)))
    if ver != 1:
        report.append("version: %s (expected 1; unchanged)" % ver)
    return report


def merge(doc: dict, root: str, profile: str, remove: bool, wiring: dict | None = None):

    """Merge the sleep hooks into a Claude-format hooks doc.

    ``wiring`` overrides the module-level ``WIRING`` when given (the registry in
    tools/agents.py drives multi-agent installs with a per-target wiring). The
    default keeps the single-file ``wire.py`` CLI acting on the measured Claude
    events. Each wiring value is ``(script, profile)``; ``profile`` \"all\" skips
    the per-entry filter so a caller can wire any event set.
    """
    hooks = doc.setdefault("hooks", {})
    report = []
    table = WIRING if wiring is None else wiring
    for event, (script, prof) in table.items():
        if profile != "all" and prof != profile:
            continue
        command = os.path.join(root, "hooks", script)
        bucket = hooks.get(event, [])
        if bucket and not shape_ok(bucket):
            report.append("%s: unrecognized shape, left alone" % event)
            continue
        # Match on the absolute script path inside the command, not on exact string
        # equality: an entry written by an older installer may wrap it (``python3 <path>``
        # or a trailing flag), and a cleanup that cannot see its own old entries leaves
        # dead config behind. The path is absolute and repo-specific, so this cannot
        # collide with someone else's hook.
        # An event's bucket is a list of entries, and one entry can hold several
        # commands. Dropping the whole entry because one of its commands is ours
        # would take somebody else's hook with it: the real ~/.omo/agent/hooks.json
        # on this machine keeps ruff-lint, golangci-lint and a leftover sleep entry in
        # a single PostToolUse entry. So prune command objects, not entries, and only
        # then drop whatever became empty.
        kept, changed = [], False
        for e in bucket:
            hooks_list = e.get("hooks")
            if not isinstance(hooks_list, list):
                kept.append(e)
                continue
            mine = [h for h in hooks_list
                    if isinstance(h, dict) and command in str(h.get("command", ""))]
            if not mine:
                kept.append(e)
                continue
            changed = True
            rest = [h for h in hooks_list if h not in mine]
            if rest:
                e = dict(e)
                e["hooks"] = rest
                kept.append(e)
        if remove:
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
            if changed:
                report.append("%s: removed %s" % (event, script))
            continue
        if changed:
            report.append("%s: already wired, unchanged" % event)
            continue
        hooks[event] = bucket + [entry(command)]
        report.append("%s: added %s" % (event, script))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wire.py")
    ap.add_argument("--settings", required=True)
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--profile", choices=["claude", "pi", "all"], default="all")
    ap.add_argument("--events", default=None, help="comma-separated event names to wire/remove (default: the profile's plus CURSOR_EVENTS)")
    ap.add_argument("--cursor", action="store_true", help="use the Cursor native hooks.json grammar (wire_cursor)")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    path = os.path.expanduser(a.settings)
    if not os.path.exists(path):
        print("  skip: %s does not exist" % path)
        return 0
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    if a.cursor:
        events = tuple(ev.strip() for ev in a.events.split(",")) if a.events else ("sessionEnd", "postToolUse")
        report = wire_cursor(doc, os.path.abspath(a.root), a.remove, events=events)
    else:
        report = merge(doc, os.path.abspath(a.root), a.profile, a.remove)
    for line in report:
        print("  %s: %s" % (os.path.basename(path), line))
    if not any(("added" in r or "removed" in r) for r in report):
        return 0
    if a.dry_run:
        print("  (dry-run) not written")
        return 0
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = "%s.bak-%s-sleep" % (path, stamp)
    shutil.copy2(path, backup)
    tmp = path + ".sleep-tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)
    print("  wrote %s (backup %s)" % (path, os.path.basename(backup)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
