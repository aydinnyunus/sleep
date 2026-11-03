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


# A Pi `input` event says where its text came from. Only typing is the human.
_HUMAN_PI_SOURCES = ("interactive",)


def provenance(payload: dict) -> str:
    """Provenance of a user-prompt payload, from the transport's own marker.

    Claude hooks only ever fire for the person at the keyboard, so a payload with
    no marker is source=user. The Pi extension can also see text that arrived in
    the user's voice but was not typed by the user (an rpc delegate notification,
    team mail, sub-agent steering): that keeps "inferred", the lowest ceiling.
    Without this split, machine-delivered text inherits the one provenance allowed
    to speak as a directive, which is exactly what safety.py exists to prevent.
    """
    marker = payload.get("pi_source")
    if marker is None:
        return "user"
    return "user" if str(marker) in _HUMAN_PI_SOURCES else "inferred"


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
    ok, detail = store.capture(text, kind=kind, source=provenance(payload),
                               session=str(payload.get("session_id") or ""),
                               cwd=str(payload.get("cwd") or ""), tags=list(tags))
    if ok:
        return "captured", detail
    if "quarantine" in detail:
        return "quarantined", detail
    return "refused", detail




_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")


def tool_outcome(payload: dict) -> str:
    """'ok' or 'failed' for a finished tool call.

    Claude reports a failed call on the response (`is_error`) and on the process
    (`interrupted`); an explicit success flag is trusted when present. When the
    payload carries neither signal the answer is "ok", because a PostToolUse event
    is delivered for a call that ran, and claiming a failure we cannot see would be
    worse than the weaker true statement.
    """
    resp = payload.get("tool_response") or payload.get("toolResponse") or {}
    if isinstance(resp, dict):
        if resp.get("is_error") or resp.get("isError"):
            return "failed"
        if resp.get("interrupted"):
            return "failed"
        if resp.get("success") is False:
            return "failed"
    if payload.get("is_error") or payload.get("isError"):
        return "failed"
    return "ok"


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
    tool = name or "tool"
    outcome = tool_outcome(payload)
    # A call without a path (Bash, Grep, and every command-shaped tool) still says
    # something worth keeping: which tool ran and whether it worked. Dropping those
    # events meant the failure that produced a wrong "fact" left no trace at all.
    if path:
        text = "session touched %s via %s (%s)" % (path, tool, outcome)
        evidence = [path, "%s %s" % (tool, outcome)]
    else:
        text = "session ran %s (%s)" % (tool, outcome)
        evidence = ["%s %s" % (tool, outcome)]
    ok, detail = store.capture(text, kind="fact", source="agent",
                               session=str(payload.get("session_id") or ""),
                               cwd=str(payload.get("cwd") or ""),
                               evidence=evidence, tags=["hook:posttool"])
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
