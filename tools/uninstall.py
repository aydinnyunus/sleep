#!/usr/bin/env python3
"""Cross-platform uninstall for the sleep cycle.

Removes every trace from the harness config (macOS / Linux / Windows):
  - Claude settings hooks (wire --profile claude --remove)
  - legacy Pi/omo hooks.json sleep entries (wire --profile pi --remove)
  - skill symlink under $CLAUDE_HOME/skills/sleep
  - the nightly scheduler job for the current OS
      macOS : launchctl unload + rm the LaunchAgent plist
      Linux : strip the crontab marker block
      Windows: schtasks /delete
  - the Pi extension symlink under $OMO_HOME/extensions/sleep

Data under ~/.sleep is NOT deleted (it holds provenance for anything you
already promoted). Pass --purge to move it to ~/.sleep-pre-purge-<ts> instead.

Mirror of tools/install.py; the same uninstall runs on macOS, Linux, Windows.
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import agents  # noqa: E402

CLAUDE_HOME = os.environ.get("CLAUDE_HOME") or os.path.join(os.path.expanduser("~"), ".claude-work")
OMO_HOME = os.environ.get("OMO_HOME") or os.path.join(os.path.expanduser("~"), ".omo", "agent")
SLEEP_HOME = os.environ.get("SLEEP_HOME") or os.path.join(os.path.expanduser("~"), ".sleep")

PLATFORM = sys.platform
IS_WIN = PLATFORM in ("win32", "cygwin")
IS_MAC = PLATFORM == "darwin"
IS_LINUX = PLATFORM.startswith("linux")

RUN = True


def run(argv):
    if not RUN:
        print("  would: " + " ".join(argv))
        return 0
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.stdout.strip():
        print("  " + r.stdout.strip().splitlines()[-1])
    return r.returncode


def step(title):
    print("\n== " + title)


def remove_symlink_or_move(path, label):
    if os.path.islink(path):
        if RUN:
            os.remove(path)
            print("  removed " + label)
        else:
            print("  would: remove " + label)
    elif os.path.isdir(path):
        dest = path + ".removed-" + time.strftime("%Y%m%dT%H%M%S")
        if RUN:
            shutil.move(path, dest)
            print("  moved " + label + " -> " + dest)
        else:
            print("  would: move " + label + " -> " + dest.replace(path, "<path>"))


def uninstall():
    step("capture hooks")
    for target in agents.ALL_TARGETS:
        if target["kind"] == "extension":
            # extension links are removed by the "extension" step below
            continue
        cfg = target["config"](os.environ)
        if not os.path.exists(cfg):
            continue
        wiring = {ev: (script, target["key"]) for ev, script in target["wiring"].items()}
        if target["kind"] == "cursor-hooks":
            run([sys.executable, os.path.join(ROOT, "tools", "wire.py"), "--settings", cfg,
                 "--cursor", "--remove"] + (["--dry-run"] if not RUN else []))
        else:
            run([sys.executable, os.path.join(ROOT, "tools", "wire.py"), "--settings", cfg,
                 "--profile", "all", "--remove", "--events", ",".join(wiring.keys())]
                + (["--dry-run"] if not RUN else []))
    # A Pi/omo hooks.json may still hold dead sleep entries from before Layer 2b.
    omo_hooks = os.path.join(OMO_HOME, "hooks.json")
    if os.path.exists(omo_hooks):
        run([sys.executable, os.path.join(ROOT, "tools", "wire.py"), "--settings", omo_hooks,
             "--profile", "pi", "--remove"] + (["--dry-run"] if not RUN else []))

    step("skill")
    remove_symlink_or_move(os.path.join(CLAUDE_HOME, "skills", "sleep"), "skill symlink")

    step("scheduler")
    if IS_MAC:
        plist = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents",
                             "com.aydinnyunus.sleep-nightly.plist")
        if os.path.exists(plist):
            run(["launchctl", "unload", plist])
            run(["rm", "-f", plist])
            print(("  would: unload + remove LaunchAgent" if not RUN else "  unloaded + removed LaunchAgent"))
        else:
            print("  no LaunchAgent plist present")
    elif IS_LINUX:
        try:
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            have = "# aydinnyunus-sleep-nightly" in r.stdout
        except FileNotFoundError:
            have = False
        if have:
            lines = [ln for ln in r.stdout.splitlines() if "# aydinnyunus-sleep-nightly" not in ln]
            body = "\n".join(lines).strip() + "\n" if lines else ""
            if RUN:
                p = subprocess.run(["crontab", "-"], input=body, capture_output=True, text=True)
                if p.returncode != 0:
                    print("  !! crontab write failed: " + p.stderr.strip())
                    return 1
            print("  removed cron marker block")
        else:
            print("  no cron nightly entry present")
    elif IS_WIN:
        run(["schtasks", "/delete", "/tn", "aydinnyunus-sleep-nightly", "/f"])
    else:
        print("  unsupported platform for scheduler; skipping")

    step("pi extension")
    remove_symlink_or_move(os.path.join(OMO_HOME, "extensions", "sleep"), "extension")

    print("\nuninstalled. promoted memory files were left exactly as they are; "
          "git revert in the memory repo is the way back.")


if __name__ == "__main__":
    argv = " ".join(sys.argv[1:])
    if "--dry-run" in argv or "--dry_run" in argv:
        RUN = False
    if "--purge" in argv:
        dest = os.path.join(os.path.expanduser("~"), ".sleep-pre-purge-" + time.strftime("%Y%m%dT%H%M%S"))
        if RUN:
            shutil.move(SLEEP_HOME, dest)
            print("moved data to " + dest)
        else:
            print("would: move %s -> %s" % (SLEEP_HOME, dest))
    sys.exit(uninstall() or 0)