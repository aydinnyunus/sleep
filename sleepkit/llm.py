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
import re
import shutil
import subprocess

from . import paths

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


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env_overrides(path: str) -> dict:
    """Read KEY=VALUE lines from an env file. A missing file means no overrides.

    Parsed here instead of sourced by a shell because this runs in the process that is
    one step from a paid API call: a shell would also execute whatever the file
    happens to contain. A malformed line raises rather than being skipped, so a typo
    costs one loud failure at the start of the night instead of an hour of silent
    "Not logged in".
    """
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return {}
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, val = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key):
            raise ValueError("%s line %d is not KEY=VALUE: %r" % (path, n, line[:40]))
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def auth_env() -> dict:
    """Environment for a headless call: this process's env, filled in from the file.

    The real environment wins over the file, so an interactive shell (or a test) is
    never overridden by a stale value on disk.
    """
    env = dict(os.environ)
    for k, v in env_overrides(paths.env_file()).items():
        env.setdefault(k, v)
    return env


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
    env = auth_env()
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
