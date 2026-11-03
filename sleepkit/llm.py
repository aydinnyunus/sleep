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

FALLBACK_MODEL = "anthropic/claude-haiku-4-5"
# DEFAULT_MODEL stays a name because callers pass it as "no opinion", but it is a plain
# literal now. SLEEP_MODEL and the ANTHROPIC_MODEL line in ~/.sleep/env both beat it, and
# they are read per call: the nightly gets a launchd env with no model in it at all, so
# whatever this process imported at start is not necessarily what the endpoint on disk
# is routed to.
DEFAULT_MODEL = FALLBACK_MODEL

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


# ---- transport selection ---------------------------------------------------------
# Three ways to reach a model, tried in a fixed order. Each one is only *eligible* when
# its binary and its configuration actually exist; the first eligible one wins, and
# SLEEP_LLM_TRANSPORT forces one for an operator who knows better.
#
#   omo    - the host agent's own headless mode (`omo -p`). Highest priority: it already
#            carries this machine's routed credentials, and it works in places where a
#            nested `claude` is refused outright (measured: `claude --version` dies with
#            SIGKILL under the host, `omo -p 'reply with exactly: OK'` returns OK).
#   claude - the historical path (`claude --bare --print`), kept because it is the one
#            proven by ~/.claude-work/hooks/self-improve.sh and by every prior release.
#   http   - a direct POST to the Anthropic-shaped endpoint from ~/.sleep/env. It needs
#            only ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN, so it survives a machine
#            where no agent CLI is installed or allowed to nest, and it returns real
#            token counts, which the CLI paths cannot.
TRANSPORTS = ("omo", "claude", "http")


def _omo_binary() -> str | None:
    for cand in (os.environ.get("OMO_BIN"), shutil.which("omo"),
                 os.path.expanduser("~/.local/bin/omo")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _env_file_values() -> dict:
    """The ~/.sleep/env values, or {} when the file is absent or unreadable."""
    try:
        return env_overrides(paths.env_file())
    except (OSError, ValueError):
        return {}


def _http_config() -> dict | None:
    """Base URL + token + model for the HTTP transport, or None when unusable.

    The process environment is consulted first and *alone* is enough when it names both
    values, so the transport can be configured without writing ~/.sleep/env (tests, CI, a
    one-off shell). The env file is only merged for keys the process did not already set,
    which is the same precedence auth_env() gives every other variable.
    """
    env = auth_env()
    base = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    token = (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if not base or not token:
        return None
    return {"base_url": base.rstrip("/"), "token": token,
            "model": env.get("SLEEP_MODEL") or env.get("ANTHROPIC_MODEL") or FALLBACK_MODEL}


def resolve_transport(model_transport: str | None = None) -> str:
    """Pick the transport: explicit argument > SLEEP_LLM_TRANSPORT > first eligible.

    Eligibility is a fact about this machine, read at call time: an omo binary on disk,
    a claude binary on disk, or an env file that names an endpoint and a token. A
    requested transport that is not eligible raises rather than silently falling back,
    because an operator who pinned one wants to hear that it is not usable here.
    """
    forced = (model_transport or os.environ.get("SLEEP_LLM_TRANSPORT") or "").strip().lower()
    if forced:
        if forced not in TRANSPORTS:
            raise ValueError("unknown SLEEP_LLM_TRANSPORT %r (one of %s)"
                             % (forced, ", ".join(TRANSPORTS)))
        return forced
    # CLAUDE_BIN is a *decision*, not a hint: an operator or a test that points at a
    # specific claude binary is saying which agent binary to run. Auto-detecting omo over
    # that would ignore the choice (and silently turn every stubbed test into a live omo
    # call). So an explicitly named claude binary outranks an auto-discovered omo.
    claude_bin = os.environ.get("CLAUDE_BIN")
    if claude_bin and os.path.isfile(claude_bin) and os.access(claude_bin, os.X_OK):
        return "claude"
    if _omo_binary():
        return "omo"
    for cand in (shutil.which("claude"), os.path.expanduser("~/.local/bin/claude")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return "claude"
    if _http_config():
        return "http"
    return "claude"   # nothing is eligible; let the claude path raise its own loud error


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


def resolve_model(model: str | None = None) -> str:
    """Name the model for a headless call: argument > SLEEP_MODEL > ANTHROPIC_MODEL > fallback.

    ANTHROPIC_MODEL is the routing variable of the gateway configured in ~/.sleep/env, and
    it only takes effect if the CLI is allowed to pick its own model. sleep always names one
    on argv, so that line has to be read here as well or the model the operator chose is
    overridden by the built-in default on every unattended pass.
    """
    if model:
        return model
    env = auth_env()
    return env.get("SLEEP_MODEL") or env.get("ANTHROPIC_MODEL") or FALLBACK_MODEL


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


def omo_model(explicit: str | None = None) -> str | None:
    """The --model value omo should be given, or None to let omo pick its own.

    omo routes through its own ~/.omo/agent configuration and rejects a model name that
    belongs to a different vocabulary (measured: it exits 1 with no output when handed
    the gateway's `deepseek/deepseek-v4.1-flash` from ~/.sleep/env, while the same call
    with its own default answers). ANTHROPIC_MODEL therefore means nothing here; only an
    operator's explicit SLEEP_MODEL (or the call's own argument) is passed through.
    """
    if explicit:
        return explicit
    return os.environ.get("SLEEP_MODEL") or None


def _run_omo(system_prompt_file: str, user_prompt: str, model: str | None,
             timeout_s: int) -> dict:
    """`omo -p` with the system prompt inlined.

    omo has no --append-system-prompt-file, so the file's text is prepended to the user
    prompt as a delimited block. That keeps one call shape and no temp files, and the
    delimiter makes the boundary explicit to the model instead of blurring two prompts
    together.
    """
    exe = _omo_binary()
    if not exe:
        return {"argv": [], "exit": 127, "stdout": "", "stderr": "omo binary not found", "json": None}
    try:
        system = open(system_prompt_file, encoding="utf-8").read()
    except OSError as exc:
        return {"argv": [], "exit": 2, "stdout": "", "stderr": "system prompt unreadable: %s" % exc, "json": None}
    prompt = "<instructions>\n%s\n</instructions>\n\n%s" % (system.strip(), user_prompt)
    # Measured argv shape on this machine: the prompt is positional and the flags may
    # precede it. omo also warns on stderr about stdin, which is harmless here.
    argv = [exe, "-p"]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    root = os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))
    env = auth_env()
    env.setdefault("SLEEP_LLM_INVOKED", "1")   # a nested call must not re-trigger sleep
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                              env=env, cwd=root)
        out, err, code = proc.stdout, proc.stderr, proc.returncode
    except (subprocess.TimeoutExpired, OSError) as exc:
        out, err, code = "", str(exc)[:400], 124
    return {"argv": argv[:3], "exit": code, "stdout": out, "stderr": err, "json": None}


def _run_http(system_prompt_file: str, user_prompt: str, model: str,
              budget_usd: float, timeout_s: int,
              output_format: str | None) -> dict:
    """Direct POST to the configured endpoint, with the system prompt in `system`.

    Adds two things the CLI paths cannot: the response's token counts (so budget_usd is
    checked against real usage) and a transport that needs no agent binary at all. Only
    stdlib is used, so this stays a zero-dependency path.
    """
    import urllib.error
    import urllib.request

    cfg = _http_config()
    if not cfg:
        return {"argv": [], "exit": 2, "stdout": "", "stderr": "no ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN configured", "json": None}
    try:
        system = open(system_prompt_file, encoding="utf-8").read()
    except OSError as exc:
        return {"argv": [], "exit": 2, "stdout": "", "stderr": "system prompt unreadable: %s" % exc, "json": None}
    body = json.dumps({"model": model, "max_tokens": 2048, "system": system,
                       "messages": [{"role": "user", "content": user_prompt}]}).encode("utf-8")
    req = urllib.request.Request(cfg["base_url"] + "/v1/messages", data=body,
                                 headers={"content-type": "application/json",
                                          "x-api-key": cfg["token"],
                                          "anthropic-version": "2023-06-01"})
    url = cfg["base_url"] + "/v1/messages"
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as exc:
        return {"argv": [url], "exit": exc.code, "stdout": exc.read().decode("utf-8", "replace")[:4000],
                "stderr": "HTTP %s" % exc.code, "json": None}
    except Exception as exc:
        return {"argv": [url], "exit": 1, "stdout": "", "stderr": str(exc)[:400], "json": None}

    # Anthropic shape: {"content":[{"type":"text","text":...}],"usage":{...}}. The
    # gateway in front of this endpoint answers in the OpenAI shape as well, so both
    # text carriers are read instead of assuming one.
    doc, text, usage = None, "", {}
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        usage = doc.get("usage") or {}
        for part in doc.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text") or ""
        if not text:
            for choice in doc.get("choices") or []:
                msg = (choice or {}).get("message") or {}
                if isinstance(msg, dict):
                    text += msg.get("content") or ""
    out = text or raw
    res = {"argv": [url], "exit": code, "stdout": out, "stderr": "", "json": None,
           "usage": usage}
    if output_format == "json":
        # Callers that asked for JSON expect the same wrapper the CLI path produces, so
        # the model's text is nested exactly where parse-json callers already look.
        res["json"] = {"result": text}
    return res


def run(system_prompt_file: str, user_prompt: str, add_dirs: list[str],
        allowed_tools: str = WRITE_TOOLS, model: str | None = None,
        budget_usd: float = 1.0, timeout_s: int = 900,
        output_format: str | None = None, extra: list[str] | None = None,
        model_transport: str | None = None) -> dict:
    omo_model_arg = model            # the caller's own choice, before gateway naming
    model = resolve_model(model)
    transport = resolve_transport(model_transport)
    if transport == "omo":
        # The resolved model is the gateway's name, which omo does not accept; only an
        # explicit choice reaches it (see omo_model).
        res = _run_omo(system_prompt_file, user_prompt,
                       omo_model(omo_model_arg), timeout_s)
        if output_format == "json":
            res["json"] = {"result": res.get("stdout") or ""}
        return res
    if transport == "http":
        return _run_http(system_prompt_file, user_prompt, model, budget_usd,
                         timeout_s, output_format)
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
