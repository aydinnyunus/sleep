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

CLAUDE_HOME = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude-work")
OMO_HOME = os.environ.get("OMO_HOME") or os.path.expanduser("~/.omo/agent")
SLEEP_HOME = os.environ.get("SLEEP_HOME") or os.path.expanduser("~/.sleep")

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


def preflight(skip_tests: bool) -> int:
    step("preflight")
    if sys.version_info < (3, 9):
        print("  needs python3 >= 3.9, found %s" % sys.version.split()[0])
        return 1
    print("  %s at %s" % (sys.version.split()[0], sys.executable or "sys.executable"))
    if not skip_tests:
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                           cwd=ROOT, capture_output=True, text=True)
        tail = (r.stdout or "").strip().splitlines()[-3:] or [r.stderr.strip()]
        for ln in tail:
            print("  " + ln)
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
    if os.path.lexists(target) and not os.path.islink(target):
        os.rename(target, target + ".bak-" + stamp())
        print("  existing %s kept aside" % target)
    if not RUN:
        print("  would: mkdir -p %s/skills" % CLAUDE_HOME)
        print("  would: ln -sfn %s  %s" % (os.path.join(ROOT, "shared-skill", "sleep"), target))
        return
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


def hooks():
    """Claude-side hooks via wire.py merge; clear dead sleep hooks from omo hooks.json."""
    step("Layer 2 - hooks")
    settings = os.path.join(CLAUDE_HOME, "settings.json")
    if os.path.isfile(settings):
        doc = json.load(open(settings, encoding="utf-8"))
        report = wire.merge(doc, ROOT, "claude", remove=False)
        for ln in report:
            print("  %s: %s" % (os.path.basename(settings), ln))
        if any("added" in r for r in report):
            _write_json_backed(settings, doc)
    else:
        print("  skip: %s does not exist" % settings)
    # Pi engine runs none of these; an entry in its hooks.json can never fire.
    # An install from before Layer 2b left one there; clear it.
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
    backup = "%s.bak-%s-sleep" % (path, stamp())
    shutil.copy2(path, backup)
    tmp = path + ".sleep-tmp"
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump(doc, h, indent=2, ensure_ascii=False)
        h.write("\n")
    os.replace(tmp, path)
    print("  wrote %s (backup %s)" % (path, os.path.basename(backup)))

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
    marker = "# aydinnyunus-sleep-nightly"
    lines = [ln for ln in cur.splitlines() if marker not in ln]
    if not RUN:
        print("  would: append cron line: %s" % cmd)
        return
    lines.append(marker)
    lines.append(cmd)
    new_cron = "\n".join(lines) + "\n"
    in_run = subprocess.run(["crontab", "-"], input=new_cron, text=True, capture_output=True)
    if in_run.returncode != 0:
        print("  crontab write failed: %s" % (in_run.stderr or r.stderr).strip())
    else:
        print("  installed cron line (03:41 daily)")


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

def extension_link(ext: bool):
    if not ext:
        return
    step("Pi extension (opt-in)")
    dest = os.path.join(OMO_HOME, "extensions", "sleep")
    if os.path.islink(dest):
        os.remove(dest)
    elif os.path.isdir(dest):
        os.rename(dest, dest + ".bak-" + stamp() + "-sleep")
    if not RUN:
        print("  would: ln -sfn %s  %s" % (os.path.join(ROOT, "extension"), dest))
        return
    os.makedirs(os.path.join(OMO_HOME, "extensions"), exist_ok=True)
    if os.path.islink(dest):
        os.remove(dest)
    try:
        os.symlink(os.path.join(ROOT, "extension"), dest, target_is_directory=True)
    except (OSError, NotImplementedError):
        shutil.copytree(os.path.join(ROOT, "extension"), dest)
    if not os.path.isfile(os.path.join(dest, "index.ts")):
        print("  linked but index.ts does not resolve - aborting")
        raise SystemExit(1)
    print("  Placed in the auto-discovered dir; it loads on the next session.")
    print("  Provenance: cd %s && omo -p 'hi' >/dev/null 2>&1; tail -4 %s/logs/extension.log" % (ROOT, SLEEP_HOME))
    print("  Expect session_start / publish / agent_settled lines there.")
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
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args(argv)
    RUN = not a.dry_run
    nightly = a.nightly or a.launchd
    ext = a.extension

    if preflight(a.skip_tests) != 0:
        return 1
    state_dirs()
    skill_link()
    hooks()

    # A user who ran plain `install.py` should not need to know the flags: ask
    # the two opt-in choices in plain language. Flags already given are honored
    # and no prompt is shown. Dry-run and non-interactive shells (CI) skip the
    # prompts and default to the unused choice, so nothing hangs and nothing
    # is scheduled without an explicit yes.
    if RUN and interactive():
        # A plain `install.py` run asks for the two opt-in choices instead of
        # demanding the flags. Non-interactive contexts (CI, scripts) skip both.
        if not nightly:
            nightly = confirm(
                "Install a daily 03:41 scheduled pass (%s)? "
                % ("launchd" if IS_MAC else "cron" if IS_LINUX else "Task Scheduler"),
                True)
        if not ext:
            ext = confirm(
                "Also install the Pi/OmO extension so sessions are captured automatically? ",
                False)

    if nightly:
        if IS_MAC:
            scheduler_mac(nightly)
        elif IS_LINUX:
            scheduler_linux(nightly)
        elif IS_WIN:
            scheduler_windows(nightly)
        else:
            print("\n== nightly\n  unsupported platform %r for scheduler; skipping" % sys.platform)
    extension_link(ext)
    done()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
