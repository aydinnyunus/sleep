#!/usr/bin/env python3
"""Cross-platform install of the sleep cycle.

Replaces install.sh. Runs on macOS, Linux, and Windows. The core (sleep.py,
sleepkit) is pure Python, so this driver only has to handle the per-OS parts:
the shared skill symlink, the Claude-side hooks (via tools/wire.py), the
nightly scheduler (launchd / cron / Task Scheduler), and the Pi extension link.

  python3 tools/install.py                  skill + hooks in the Claude settings, backups;
                                            prompts for the scheduler and extension
                                            (answered directly, no flags needed)
  python3 tools/install.py --dry-run        print every action, change nothing
  python3 tools/install.py --nightly        also install the daily scheduler pass (no prompt)
  python3 tools/install.py --extension      also link the Pi extension (no prompt)
  python3 tools/install.py --skip-tests     don't run the test suite first

When --nightly / --extension are omitted and the terminal is interactive, the
installer asks whether to set up each one, so a user never has to know the
flags. In a non-interactive shell (CI) it skips both, keeping scripts safe.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
# wire.py is a plain module: import its merge() so this driver keeps backups,
# dry-run shaping, and the event wiring in exactly one place.
import wire  # noqa: E402
import agents  # noqa: E402

CLAUDE_HOME = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude-work")
OMO_HOME = os.environ.get("OMO_HOME") or os.path.expanduser("~/.omo/agent")
SLEEP_HOME = os.environ.get("SLEEP_HOME") or os.path.expanduser("~/.sleep")

# Agents selected for capture wiring. Default is Claude Code only (preserves the
# historical no-flag contract); the user can extend this via --agents or the
# interactive prompt.
_SELECTED = ["claude"]


def _selected_agents():
    return list(_SELECTED)


PLATFORM = sys.platform  # darwin | linux | win32
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

RUN = True  # flipped by --dry-run


def run(argv):
    if not RUN:
        print("  would: %s" % " ".join(str(a) for a in argv))
        return None
    res = subprocess.run(argv, capture_output=True, text=True)
    if res.stdout.strip():
        print("  " + res.stdout.strip().replace("\n", "\n  "))
    if res.returncode != 0 and res.stderr.strip():
        print("  (stderr) " + res.stderr.strip().replace("\n", "\n  (stderr) "))
    return res.returncode


def step(title):
    print("\n== %s" % title)


def stamp():
    return _dt.datetime.now().strftime("%Y%m%dT%H%M%S")


def interactive() -> bool:
    """True when stdin is a real terminal, so we can ask Y/n questions."""
    return getattr(sys.stdin, "isatty", lambda: False)()


def confirm(question: str, default: bool) -> bool:
    """Ask one Y/n question. Returns default when stdin is not a terminal.

    Flags given on argv already locked the choice before this is called, so a
    prompt only ever appears for a choice the user did not make explicit.
    """
    if not interactive():
        return default
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        ans = input(question + suffix).strip().lower()
        if ans in ("", "y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please answer y or n")


def _ask_agents() -> list:
    """Ask which agents to wire the capture into. Default: Claude Code only.

    Returns the list of agent keys. ``--agents`` on argv already locked the choice
    before this is called; without it, a real terminal asks, and a non-interactive
    shell keeps the default (claude) so scripts and CI never hang.
    """
    if not interactive():
        return _selected_agents()
    labels = agents.target_labels()
    keys = agents.known_keys()
    print("  Which AI coding agents should capture sessions into the sleep cycle?")
    print("  (default: %s)" % labels["claude"])
    for i, k in enumerate(keys, 1):
        print("    %d) %-12s %s" % (i, k, labels[k]))
    while True:
        ans = input("  Agents to install (e.g. 1,3, or numbers; Enter = 1): ").strip()
        if ans == "":
            return ["claude"]
        sel = []
        bad = []
        for tok in ans.replace(";", ",").split(","):
            tok = tok.strip().lower()
            if not tok:
                continue
            if tok in keys:
                sel.append(tok)
                continue
            if tok.isdigit():
                idx = int(tok)
                if 1 <= idx <= len(keys):
                    sel.append(keys[idx - 1])
                    continue
            bad.append(tok)
        if bad:
            print("  unknown selection, try again: %s" % ", ".join(bad))
            continue
        # de-dup preserving order; claude is always meaningful on its own
        seen, out = set(), []
        for k in sel:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out or ["claude"]


def _resolve_agents(explicit, interactive_prompt: bool) -> None:
    """Set the module _SELECTED list from --agents, or the interactive prompt, or the default."""
    global _SELECTED
    if explicit is not None:
        vals = [v.strip().lower() for v in explicit.split(",") if v.strip()]
        bad = [v for v in vals if v not in agents.known_keys()]
        if bad:
            raise SystemExit("unknown flag value for --agents: %s (known: %s)" % (", ".join(bad), ", ".join(agents.known_keys())))
        _SELECTED = vals or ["claude"]
    elif interactive_prompt:
        _SELECTED = _ask_agents()


def preflight(skip_tests: bool) -> int:
    step("preflight")
    if sys.version_info < (3, 9):
        print("  needs python3 >= 3.9, found %s" % sys.version.split()[0])
        return 1
    print("  %s at %s" % (sys.version.split()[0], sys.executable or "sys.executable"))
    if not skip_tests:
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                           cwd=ROOT, capture_output=True, text=True)
        combined = "\n".join(part for part in (r.stdout, r.stderr) if part).strip().splitlines()
        for ln in combined[-5:]:
            print("  " + ln)
        if r.returncode != 0:
            print("  test gate failed with exit %s; install aborted" % r.returncode)
            return r.returncode
    return 0

def state_dirs():
    step("state dirs")
    if not RUN:
        print("  would: mkdir -p %s" % SLEEP_HOME)
        return
    os.makedirs(SLEEP_HOME, exist_ok=True)


def skill_link():
    step("Layer 1 - skill")
    target = os.path.join(CLAUDE_HOME, "skills", "sleep")
    if not RUN:
        if os.path.lexists(target) and not os.path.islink(target):
            print("  would: move existing %s aside" % target)
        print("  would: mkdir -p %s/skills" % CLAUDE_HOME)
        print("  would: ln -sfn %s  %s" % (os.path.join(ROOT, "shared-skill", "sleep"), target))
        return
    if os.path.lexists(target) and not os.path.islink(target):
        os.rename(target, target + ".bak-" + stamp())
        print("  existing %s kept aside" % target)
    os.makedirs(os.path.join(CLAUDE_HOME, "skills"), exist_ok=True)
    if os.path.islink(target):
        os.remove(target)
    elif os.path.isdir(target):
        os.rename(target, target + ".bak-" + stamp())
    try:
        os.symlink(os.path.join(ROOT, "shared-skill", "sleep"), target, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows without symlink privilege: fall back to a copy.
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(os.path.join(ROOT, "shared-skill", "sleep"), target)
    print("  %s -> %s  (shared skill root)" % (target, os.path.join(ROOT, "shared-skill", "sleep")))


def wire_shell_hook(target: dict):
    """Wire a shell-hook target (Claude-format settings.json) via wire.py merge."""
    cfg = target["config"](os.environ)
    if not os.path.isfile(cfg):
        print("  skip: %s does not exist" % cfg)
        return
    doc = json.load(open(cfg, encoding="utf-8"))
    wiring = {ev: (script, target["key"]) for ev, script in target["wiring"].items()}
    report = wire.merge(doc, ROOT, "all", remove=False, wiring=wiring)
    for ln in report:
        print("  %s: %s" % (os.path.basename(cfg), ln))
    if any("added" in r for r in report):
        _write_json_backed(cfg, doc)


def wire_cursor_hook(target: dict):
    """Wire a Cursor-native hooks.json (camelCase grammar) via wire.wire_cursor."""
    cfg = target["config"](os.environ)
    if not os.path.isfile(cfg):
        print("  skip: %s does not exist" % cfg)
        return
    doc = json.load(open(cfg, encoding="utf-8"))
    report = wire.wire_cursor(doc, ROOT, remove=False, events=tuple(target["wiring"].keys()))
    for ln in report:
        print("  %s: %s" % (os.path.basename(cfg), ln))
    if any("added" in r for r in report):
        _write_json_backed(cfg, doc)


def _opencode_project():
    """Resolve the OpenCode project root: $SLEEP_PROJECT, else the cwd (this is a
    project-scoped plugin, unlike the global config of the other agents)."""
    explicit = os.environ.get("SLEEP_PROJECT")
    if explicit:
        return os.path.abspath(explicit)
    # prefer a cwd that actually has .opencode, otherwise the install cwd
    return os.getcwd()


def wire_opencode_hook(target: dict):
    """Install and register the OpenCode plugin inside the selected project."""
    project = _opencode_project()
    plugin_dir = os.path.join(project, ".opencode", "plugins")
    dest = os.path.join(plugin_dir, "sleep-opencode.ts")
    src_file = target["plugin_src"]
    if not os.path.isfile(src_file):
        print("  skip: %s missing (repo plugin not found)" % src_file)
        return
    source = open(src_file, encoding="utf-8").read()
    rendered = (source
                .replace(json.dumps("__SLEEP_REPO__"), json.dumps(ROOT))
                .replace(json.dumps("__SLEEP_PYTHON__"), json.dumps(sys.executable or "python3")))
    if RUN:
        os.makedirs(plugin_dir, exist_ok=True)
        current = open(dest, encoding="utf-8").read() if os.path.isfile(dest) else None
        if current != rendered:
            with open(dest, "w", encoding="utf-8") as h:
                h.write(rendered)
            print("  wrote plugin %s" % dest)
        else:
            print("  %s already matches, unchanged" % dest)
    else:
        print("  would: render %s -> %s" % (src_file, dest))

    cfg = os.path.join(project, ".opencode", "opencode.json")
    doc = json.load(open(cfg, encoding="utf-8")) if os.path.isfile(cfg) else {}
    runs = doc.get("plugin") or []
    if not isinstance(runs, list):
        print("  skip: %s plugin key is not a list" % cfg)
        return
    entry = "./.opencode/plugins"
    if entry not in runs:
        doc["plugin"] = runs + [entry]
        _write_json_backed(cfg, doc)
        print("  registered plugin dir in %s" % cfg)
    else:
        print("  %s already registers ./.opencode/plugins" % cfg)


def wire_windsurf_hook(target: dict):
    """Windsurf ~/.codeium/windsurf/hooks.json (bare {command} entries)."""
    cfg = target["config"](os.environ)
    if not os.path.isfile(cfg):
        print("  skip: %s does not exist" % cfg)
        return
    doc = json.load(open(cfg, encoding="utf-8"))
    report = wire.wire_windsurf(doc, ROOT, remove=False, events=tuple(target["wiring"].keys()))
    for ln in report:
        print("  %s: %s" % (os.path.basename(cfg), ln))
    if any("added" in r for r in report):
        _write_json_backed(cfg, doc)


def wire_cline_hook(target: dict):
    """Cline VS Code user settings (cline.execute.actions map)."""
    cfg = target["config"](os.environ)
    if not os.path.isfile(cfg):
        print("  skip: %s does not exist" % cfg)
        return
    doc = json.load(open(cfg, encoding="utf-8"))
    report = wire.wire_cline(doc, ROOT, remove=False, events=tuple(target["wiring"].keys()))
    for ln in report:
        print("  %s: %s" % (os.path.basename(cfg), ln))
    if any("added" in r for r in report):
        _write_json_backed(cfg, doc)


def wire_codex_hook(target: dict):
    """Codex ~/.codex/config.toml [notify] block (TOML, not JSON)."""
    cfg = target["config"](os.environ)
    if not os.path.isfile(cfg):
        print("  skip: %s does not exist" % cfg)
        return
    text = open(cfg, encoding="utf-8").read()
    msgs, new_text, changed = wire.wire_codex(text, ROOT, remove=False)
    for ln in msgs:
        print("  %s: %s" % (os.path.basename(cfg), ln))
    if changed and RUN:
        backup = "%s.bak-%s-sleep" % (cfg, stamp())
        shutil.copy2(cfg, backup)
        with open(cfg + ".sleep-tmp", "w", encoding="utf-8") as h:
            h.write(new_text)
        os.replace(cfg + ".sleep-tmp", cfg)
        print("  wrote %s (backup %s)" % (os.path.basename(cfg), os.path.basename(backup)))
    elif changed and not RUN:
        print("  (dry-run) not written")


def hooks():
    """Wire the requested agents' capture hooks, driven by the tools/agents registry.

    The user chooses the agents at install time (tools/install.py --agents, or an
    interactive prompt). Each target routes by kind: shell-hook (Claude-format
    settings.json) through wire.merge, cursor-hooks (native camelCase grammar)
    through wire.wire_cursor. Pi/OmO is an extension, not JSON-hook shaped, and is
    linked separately by extension_link(). A Pi hooks.json entry is dead config and
    is always cleared here.
    """
    step("Layer 2 - capture hooks")
    sel = _selected_agents()
    for key in sel:
        target = agents.by_key(key)
        if target is None:
            print("  skip: unknown agent %r" % key)
            continue
        if target["kind"] == "shell-hook":
            wire_shell_hook(target)
        elif target["kind"] == "cursor-hooks":
            wire_cursor_hook(target)
        elif target["kind"] == "opencode-plugin":
            wire_opencode_hook(target)
        elif target["kind"] == "windsurf-hooks":
            wire_windsurf_hook(target)
        elif target["kind"] == "cline-actions":
            wire_cline_hook(target)
        elif target["kind"] == "codex-notify":
            wire_codex_hook(target)
        elif target["kind"] == "extension":
            print("  %s: extension, handled by `extension` step" % target["label"])
        else:
            print("  skip: %s: unsupported kind %r" % (key, target["kind"]))
    # Pi engine runs none of the Claude events; an entry in omo hooks.json can never
    # fire. An install from before Layer 2b left one there; always clear it.
    omo = os.path.join(OMO_HOME, "hooks.json")
    if os.path.isfile(omo):
        doc = json.load(open(omo, encoding="utf-8"))
        report = wire.merge(doc, ROOT, "all", remove=True)
        for ln in report:
            print("  %s: %s" % (os.path.basename(omo), ln))
        if any("removed" in r for r in report):
            _write_json_backed(omo, doc)


def _write_json_backed(path, doc):
    if not RUN:
        print("  (dry-run) not written")
        return
    existed = os.path.isfile(path)
    backup = "%s.bak-%s-sleep" % (path, stamp())
    if existed:
        shutil.copy2(path, backup)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".sleep-tmp"
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump(doc, h, indent=2, ensure_ascii=False)
        h.write("\n")
    os.replace(tmp, path)
    print("  wrote %s%s" % (path, " (backup %s)" % os.path.basename(backup) if existed else ""))

# --- nightly scheduler per OS ---

NIGHTLY_CMD = os.path.join(ROOT, "sleep.py") + " nightly --min-records 2 --min-interval 0 --budget 1.00 --replay-interval 168 --replay-limit 3 --replay-budget 0.50"


def scheduler_mac(nightly: bool):
    step("nightly pass (03:41, launchd)")
    src = os.path.join(ROOT, "launchd", "com.aydinnyunus.sleep-nightly.plist")
    dst = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.aydinnyunus.sleep-nightly.plist")
    if not RUN:
        print("  would: render %s -> %s and load it" % (src, dst))
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    plist = open(src, encoding="utf-8").read()
    plist = (plist.replace("__SLEEPCTL__", os.path.join(ROOT, "sleep.py"))
                   .replace("__SLEEPHOME__", SLEEP_HOME)
                   .replace("__USERHOME__", os.path.expanduser("~"))
                   .replace("/usr/bin/python3", sys.executable))
    with open(dst, "w", encoding="utf-8") as h:
        h.write(plist)
    # Strict parse gate: plutil/launchctl accepted a file plistlib could not.
    import plistlib
    try:
        plistlib.loads(open(dst, "rb").read())
    except Exception as e:
        print("  REFUSED: the rendered plist does not parse (%s): %s" % (dst, e))
        return
    if os.environ.get("SLEEP_SKIP_LOAD") == "1":
        print("  rendered and linted %s (SLEEP_SKIP_LOAD=1: not loaded)" % dst)
        return
    # launchctl registers in the logged-in user's domain; HOME redirection does not move it.
    if home_is_real():
        subprocess.run(["launchctl", "unload", dst], capture_output=True)
        r = subprocess.run(["launchctl", "load", dst], capture_output=True, text=True)
        if r.returncode != 0:
            print("  launchctl load returned %s: %s" % (r.returncode, r.stderr.strip()))
        else:
            print("  loaded %s" % dst)
    else:
        print("  rendered %s, NOT loaded: HOME (%s) is not the real home." % (dst, os.getenv("HOME")))
        print("  launchctl load \"%s\"" % dst)
    if not os.path.isfile(os.path.join(SLEEP_HOME, "env")):
        print("  note: %s/env is absent. If the model needs a login token," % SLEEP_HOME)
        print("        write KEY=VALUE lines there and chmod 600; the nightly reads it.")


def home_is_real() -> bool:
    try:
        import pwd
        return os.path.expanduser("~") == pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return True


CRON_BEGIN = "# BEGIN aydinnyunus-sleep-nightly"
CRON_END = "# END aydinnyunus-sleep-nightly"
CRON_LEGACY_MARKER = "# aydinnyunus-sleep-nightly"


def _strip_sleep_cron(lines):
    """Remove only complete owned blocks plus the legacy marker/command pair."""
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line == CRON_BEGIN:
            try:
                end = lines.index(CRON_END, i + 1)
            except ValueError as exc:
                raise ValueError("unterminated sleep cron block") from exc
            i = end + 1
            continue
        if line == CRON_LEGACY_MARKER:
            i += 1
            if i < len(lines) and "sleep.py" in lines[i] and "nightly" in lines[i]:
                i += 1
            continue
        out.append(line)
        i += 1
    return out


def scheduler_linux(_nightly: bool):
    step("nightly pass (03:41, cron)")
    tmpl = open(os.path.join(ROOT, "schedulers", "nightly.cron"), encoding="utf-8").read()
    cmd = "3 41 * * * %s > %s 2>&1" % (NIGHTLY_CMD, os.path.join(SLEEP_HOME, "logs", "nightly.log"))
    tmpl = tmpl.replace("__SLEEP_CRON__", cmd)
    # read current crontab
    cur = ""
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if r.returncode == 0:
        cur = r.stdout
    try:
        lines = _strip_sleep_cron(cur.splitlines())
    except ValueError as exc:
        print("  refused cron update: %s" % exc)
        return 1
    if not RUN:
        print("  would: replace owned cron block with: %s" % cmd)
        return 0
    lines.extend((CRON_BEGIN, cmd, CRON_END))
    new_cron = "\n".join(lines) + "\n"
    in_run = subprocess.run(["crontab", "-"], input=new_cron, text=True, capture_output=True)
    if in_run.returncode != 0:
        print("  crontab write failed: %s" % (in_run.stderr or r.stderr).strip())
    else:
        print("  installed cron line (03:41 daily)")
        return 0
    return 1


def scheduler_windows(_nightly: bool):
    step("nightly pass (03:41, Task Scheduler)")
    tmpl = open(os.path.join(ROOT, "schedulers", "nightly-windows.cmd"), encoding="utf-8").read()
    tn = "aydinnyunus-sleep-nightly"
    action = "%s %s" % (sys.executable, NIGHTLY_CMD)
    tmpl = tmpl.replace("__SLEEP_SCHTASKS_CMD__", action)
    if not RUN:
        print("  would: schtasks /create /tn %s /tr ... /sc daily /st 03:41" % tn)
        return
    r = subprocess.run([
        "schtasks", "/create", "/tn", tn,
        "/tr", action, "/sc", "daily", "/st", "03:41", "/f",
    ], capture_output=True, text=True)
    if r.returncode != 0:
        print("  schtasks create failed: %s" % (r.stderr or r.stdout).strip())
    else:
        print("  scheduled %s (03:41 daily)" % tn)

def extension_link(selected_agents):
    """Link the TypeScript capture extension into each selected extension-kind agent's
    auto-discovered extensions dir (pi -> ~/.omo/agent/extensions, oh-my-pi -> ~/.omp/agent).
    A hooks.json entry can never fire on these engines; only the extension link matters."""
    targets = [t for t in agents.EXTENSION_TARGETS if t["key"] in selected_agents]
    if not targets:
        return
    step("extension link")
    for target in targets:
        dest = os.path.join(target["extension_dir"](os.environ), "sleep")
        source = os.path.join(ROOT, "extension")
        label = "%s extension" % target["key"]
        if os.path.islink(dest) and os.path.realpath(dest) == os.path.realpath(source):
            print("  %s already linked, unchanged" % label)
            continue
        if not RUN:
            print("  would: replace %s with link to %s" % (dest, source))
            continue
        if os.path.islink(dest):
            os.remove(dest)
        elif os.path.isdir(dest):
            os.rename(dest, dest + ".bak-" + stamp() + "-sleep")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.symlink(source, dest, target_is_directory=True)
        except (OSError, NotImplementedError):
            shutil.copytree(source, dest)
        if not os.path.isfile(os.path.join(dest, "index.ts")):
            print("  linked but index.ts does not resolve - aborting")
            raise SystemExit(1)
        home_dir = target["extension_dir"](os.environ)
        print("  Placed %s in %s (loads on the next session)" % (label, home_dir))
    print("  Provenance: cd %s && omo -p 'hi' >/dev/null 2>&1; tail -4 %s/logs/extension.log" % (ROOT, SLEEP_HOME))
    print("  Kill switch: SLEEP_DISABLE=1. Uninstall: %s/uninstall.sh" % ROOT)


def done():
    step("done")
    print("  Next:")
    print("    %s/sleep.py status -v" % ROOT)
    print("    %s/sleep.py sleep --dry-run" % ROOT)
    print("    %s/sleep.py replay --limit 3   # dry-run plan; --run costs model budget" % ROOT)
    print("  Revert everything: %s/uninstall.sh" % ROOT)


class _Parser(argparse.ArgumentParser):
    """Argparse fork that reports unrecognized flags as 'unknown flag' so scripts
    can detect a typo'd flag loudly (exit 2), matching the old install.sh contract."""

    def error(self, message):
        if message.startswith("unrecognized arguments:"):
            args = message.split(":", 1)[1].strip()
            self.exit(2, "unknown flag: %s\n" % args)
        super().error(message)


def main(argv=None) -> int:
    global RUN
    ap = _Parser(prog="tools/install.py", description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    ap.add_argument("--nightly", action="store_true", help="also install the daily scheduler pass (launchd/cron/schtasks)")
    ap.add_argument("--launchd", action="store_true", help="alias for --nightly (macOS convention kept)")
    ap.add_argument("--extension", action="store_true")
    ap.add_argument("--agents", metavar="LIST", help="comma-separated agent keys to wire (e.g. claude,pi,cursor); default claude. Prompted interactively if omitted.")
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args(argv)
    RUN = not a.dry_run
    nightly = a.nightly or a.launchd
    ext = a.extension

    if preflight(a.skip_tests) != 0:
        return 1
    state_dirs()
    skill_link()

    # Resolve which agents to wire BEFORE the hook step. A real terminal that left
    # --agents off is asked (multi-select). Non-interactive shells (CI, scripts)
    # default to claude only, preserving the historical contract.
    _resolve_agents(a.agents, RUN and interactive())
    ext = ext or "pi" in _selected_agents()
    hooks()

    # A user who ran plain `install.py` should not need to know the flags: ask
    # the remaining opt-in choices in plain language. Flags already given are
    # honored and no prompt is shown. Dry-run and non-interactive shells (CI) skip
    # the prompts and default to the unused choice, so nothing hangs and nothing
    # is scheduled without an explicit yes.
    if RUN and interactive():
        if not nightly:
            nightly = confirm(
                "Install a daily 03:41 scheduled pass (%s)? "
                % ("launchd" if IS_MAC else "cron" if IS_LINUX else "Task Scheduler"),
                True)
        if not ext:
            ext = confirm(
                "Also install the Pi/OmO extension so sessions are captured automatically? ",
                False)
    # --extension (or the interactive yes) preserves the historic Pi opt-in.
    # Oh My Pi remains an independent --agents oh-my-pi selection.
    if ext and "pi" not in _selected_agents():
        _SELECTED.append("pi")

    if nightly:
        if IS_MAC:
            scheduler_mac(nightly)
        elif IS_LINUX:
            if scheduler_linux(nightly) != 0:
                return 1
        elif IS_WIN:
            scheduler_windows(nightly)
        else:
            print("\n== nightly\n  unsupported platform %r for scheduler; skipping" % sys.platform)
    extension_link(_selected_agents())
    done()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
