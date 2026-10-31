"""Headless LLM boundary.

Deliberately mirrors the invocation already proven on this machine by
~/.claude-work/hooks/self-improve.sh (claude --bare --print, haiku, budget cap,
--no-session-persistence, scoped --allowed-tools). Reusing a known-good shape
beats inventing a new one.

The model writes files itself through the scoped tool whitelist; we do not ask
it to emit JSON we then parse, because that adds a failure mode for no gain.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

DEFAULT_MODEL = os.environ.get("SLEEP_MODEL", "anthropic/claude-haiku-4-5")

# Read + write only inside the dirs we hand it. No Bash, so a bad distillation
# cannot shell out.
WRITE_TOOLS = "Read,Write,Edit,Glob,Grep"
READONLY_TOOLS = "Read,Glob,Grep"


def binary() -> str:
    for cand in (os.environ.get("CLAUDE_BIN"), shutil.which("claude"),
                 os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude",
                 "/opt/homebrew/bin/claude"):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise FileNotFoundError("claude binary not found; set CLAUDE_BIN")


def parse_json_output(text: str):
    """Read whatever the CLI printed as JSON.

    `claude --output-format json` has shipped as one object and as an array of turn
    objects, and a noisy prefix line can precede either. Whole-document first, then
    the last line that parses on its own: silently returning None here would make
    every metric in a report read "n/a" and nobody would notice.
    """
    t = (text or "").strip()
    if not t:
        return None
    try:
        return json.loads(t)
    except ValueError:
        pass
    last = None
    for line in t.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except ValueError:
            pass
    return last


def run(system_prompt_file: str, user_prompt: str, add_dirs: list[str],
        allowed_tools: str = WRITE_TOOLS, model: str = DEFAULT_MODEL,
        budget_usd: float = 1.0, timeout_s: int = 900,
        output_format: str | None = None, extra: list[str] | None = None) -> dict:
    exe = ["claude"]
    try:
        exe = [binary()]
    except FileNotFoundError:
        pass
    argv = exe + ["--bare", "--print", "--model", model,
                  "--max-budget-usd", "%.2f" % budget_usd,
                  "--no-session-persistence",
                  "--append-system-prompt-file", system_prompt_file]
    if output_format:
        argv += ["--output-format", output_format]
    for d in add_dirs:
        argv += ["--add-dir", d]
    argv += ["--allowed-tools", allowed_tools]
    argv += list(extra or [])
    env = dict(os.environ)
    env["CLAUDE_SELF_IMPROVE_ACTIVE"] = "1"   # do not trigger the other pipeline
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    try:
        # input= already implies stdin=PIPE; passing both is a ValueError.
        proc = subprocess.run(argv, input=user_prompt, capture_output=True, text=True,
                              timeout=timeout_s, env=env)
        out, err, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        out, err, code = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""), "timeout", 124
    parsed = None
    if output_format == "json":
        try:
            parsed = parse_json_output(out)
        except Exception:
            parsed = None
    return {"argv": argv[:6], "exit": code, "stdout": out, "stderr": err, "json": parsed}
