"""Stdin-JSON adapters for the harness hooks.

Claude-format hooks hand a JSON object on stdin. Parsing it here rather than in
the shell keeps the parsing testable and keeps the hook scripts to one line, so
there is no chance of a quoting bug deciding what enters memory.
"""
from __future__ import annotations

import json
import os
import sys

from . import paths, store

MAX_CHARS = 8000
FIRE_LOG_LIMIT = 256 * 1024
_TEXT_KEYS = ("prompt", "user_prompt", "message", "userMessage")


def note_fire(event: str, raw: str) -> None:
    """Count every hook invocation, including the ones we then ignore.

    Without this, "no buffer record" cannot be told apart from "the hook never
    ran": a path-less tool event used to vanish with no trace at all, and on
    this box that made a whole layer unmeasurable. One compact line per fire,
    size-capped to one generation. Never raises - a hook must not fail because
    its accounting did.
    """
    try:
        p = os.path.join(paths.logs_dir(), "hook-fires.log")
        if os.path.exists(p) and os.path.getsize(p) > FIRE_LOG_LIMIT:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\tbytes=%d\n" % (paths.now(), event, len(raw)))
    except Exception:
        pass


def _drain(stream, event: str) -> str:
    """Read stdin once - we need the byte count - and record the fire."""
    raw = stream.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    note_fire(event, raw or "")
    return raw or ""


def user_text(payload: dict) -> str:
    """Best-effort extraction of the human's own words from a hook payload."""
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):  # content-block form
            parts = [str(b.get("text", "")) for b in value if isinstance(b, dict)]
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                return joined
    return ""


def capture_from_stdin(stdin=None, kind: str = "preference",
                       tags: tuple[str, ...] = ("hook:userprompt",)) -> tuple[str, str]:
    """Returns (outcome, detail); outcome is captured|refused|quarantined|ignored."""
    stream = stdin if stdin is not None else sys.stdin
    raw = _drain(stream, "userprompt")
    try:
        payload = json.loads(raw)
    except Exception:
        return "ignored", "unparseable hook payload"
    if not isinstance(payload, dict):
        return "ignored", "hook payload not an object"
    text = user_text(payload)
    if not text:
        return "ignored", "no user text in payload"
    if len(text) > MAX_CHARS:
        return "ignored", "payload too large"
    ok, detail = store.capture(text, kind=kind, source="user",
                               session=str(payload.get("session_id") or ""),
                               cwd=str(payload.get("cwd") or ""), tags=list(tags))
    if ok:
        return "captured", detail
    if "quarantine" in detail:
        return "quarantined", detail
    return "refused", detail




_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")


def tool_event(payload: dict) -> tuple[str, str]:
    """(tool_name, path) from a PostToolUse payload, or ("", "")."""
    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    data = payload.get("tool_input") or payload.get("toolInput") or {}
    if isinstance(data, dict):
        for key in _PATH_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return name, value.strip()
    return name, ""


def capture_tool_from_stdin(stdin=None) -> tuple[str, str]:
    """PostToolUse trace: which file this session touched. source=agent, always -
    a tool event is never the user's own statement, and must not be able to claim
    that provenance just because a hook delivered it."""
    stream = stdin if stdin is not None else sys.stdin
    raw = _drain(stream, "posttool")
    try:
        payload = json.loads(raw)
    except Exception:
        return "ignored", "unparseable hook payload"
    if not isinstance(payload, dict):
        return "ignored", "hook payload not an object"
    name, path = tool_event(payload)
    if not path:
        return "ignored", "no path in tool event"
    text = "session touched %s via %s" % (path, name or "tool")
    ok, detail = store.capture(text, kind="fact", source="agent",
                               session=str(payload.get("session_id") or ""),
                               cwd=str(payload.get("cwd") or ""),
                               evidence=[path], tags=["hook:posttool"])
    return ("captured" if ok else "refused"), detail


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "capture"
    if mode == "capture-tool":
        outcome, detail = capture_tool_from_stdin()
    elif mode == "capture":
        outcome, detail = capture_from_stdin()
        # stdout of a hook can be injected into context, so say nothing.
        sys.stderr.write("%s %s\n" % (outcome, detail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
