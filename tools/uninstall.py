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
import json
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


def write_json_backed(path, doc):
    if not RUN:
        print("  would: update %s" % path)
        return
    backup = "%s.bak-%s-sleep" % (path, time.strftime("%Y%m%dT%H%M%S"))
    shutil.copy2(path, backup)
    tmp = path + ".sleep-tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)
    print("  updated %s (backup %s)" % (path, os.path.basename(backup)))


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


def remove_scheduler_linux():
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        print("  no cron nightly entry present")
        return 0
    current = result.stdout if result.returncode == 0 else ""
    original = current.splitlines()
    try:
        kept = _strip_sleep_cron(original)
    except ValueError as exc:
        print("  refused cron cleanup: %s" % exc)
        return 1
    if kept == original:
        print("  no cron nightly entry present")
        return 0
    body = "\n".join(kept).strip() + "\n" if kept else ""
    if RUN:
        import tempfile as _tempfile
        fd, tmp_cron = _tempfile.mkstemp(prefix="sleep-cron-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            written = subprocess.run(["crontab", tmp_cron], capture_output=True, text=True)
        finally:
            try:
                os.remove(tmp_cron)
            except OSError:
                pass
        if written.returncode != 0:
            print("  !! crontab write failed: " + written.stderr.strip())
            return 1
    print("  would: remove cron marker block" if not RUN else "  removed cron marker block")
    return 0


def uninstall():
    step("capture hooks")
    for target in agents.ALL_TARGETS:
        kind = target["kind"]
        if kind in ("extension", "opencode-plugin"):
            # links/plugin files are removed by later steps below
            continue
        cfg = target["config"](os.environ)
        if not os.path.exists(cfg):
            continue
        wiring = {ev: (script, target["key"]) for ev, script in target["wiring"].items()}
        if kind == "cursor-hooks":
            run([sys.executable, os.path.join(ROOT, "tools", "wire.py"), "--settings", cfg,
                 "--cursor", "--remove"] + (["--dry-run"] if not RUN else []))
        elif kind in ("windsurf-hooks", "cline-actions", "codex-notify"):
            run([sys.executable, os.path.join(ROOT, "tools", "wire.py"), "--settings", cfg,
                 "--kind", {"windsurf-hooks": "windsurf", "cline-actions": "cline", "codex-notify": "codex"}[kind],
                 "--remove"] + (["--dry-run"] if not RUN else []))
        else:
            run([sys.executable, os.path.join(ROOT, "tools", "wire.py"), "--settings", cfg,
                 "--profile", "all", "--remove", "--events", ",".join(wiring.keys())]
                + (["--dry-run"] if not RUN else []))
    # A Pi/omo hooks.json may still hold dead sleep entries from before Layer 2b.
    omo_hooks = os.path.join(OMO_HOME, "hooks.json")
    if os.path.exists(omo_hooks):
        run([sys.executable, os.path.join(ROOT, "tools", "wire.py"), "--settings", omo_hooks,
             "--profile", "pi", "--remove"] + (["--dry-run"] if not RUN else []))

    step("opencode plugin")
    opencode = agents.by_key("opencode")
    if opencode is not None:
        project = os.environ.get("SLEEP_PROJECT") or os.getcwd()
        plugin_file = opencode["plugin_dest"](project)
        if os.path.isfile(plugin_file):
            if RUN:
                os.remove(plugin_file)
                print("  removed opencode plugin")
            else:
                print("  would: remove opencode plugin")
        # Current installs are project-scoped. Also clean the old global path for
        # installs made by the pre-fix implementation.
        configs = [
            os.path.join(project, ".opencode", "opencode.json"),
            opencode["config"](os.environ),
        ]
        for oc_conf in dict.fromkeys(configs):
            if not os.path.isfile(oc_conf):
                continue
            try:
                doc = json.load(open(oc_conf, encoding="utf-8"))
                runs = doc.get("plugin") or []
                if isinstance(runs, list) and "./.opencode/plugins" in runs:
                    doc["plugin"] = [e for e in runs if e != "./.opencode/plugins"]
                    write_json_backed(oc_conf, doc)
                    print("  unregistered plugin dir from %s" % oc_conf)
            except (OSError, ValueError) as exc:
                print("  skip opencode.json unregister: %s" % exc)

    step("skill")
    remove_symlink_or_move(os.path.join(CLAUDE_HOME, "skills", "sleep"), "skill symlink")

    step("scheduler")
    if IS_MAC:
        plist = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents",
                             "com.aydinnyunus.sleep-nightly.plist")
        if os.path.exists(plist):
            # bootout, not `unload <path>`: unload can report success while the job
            # launchd is running under a different path survives (measured 2026-11-05,
            # a plist that `load` accepted and never registered).
            run(["launchctl", "bootout", "gui/%d" % os.getuid()])
            run(["launchctl", "unload", plist])
            run(["rm", "-f", plist])
            print(("  would: unload + remove LaunchAgent" if not RUN else "  unloaded + removed LaunchAgent"))
        else:
            print("  no LaunchAgent plist present")
    elif IS_LINUX:
        if remove_scheduler_linux() != 0:
            return 1
    elif IS_WIN:
        run(["schtasks", "/delete", "/tn", "aydinnyunus-sleep-nightly", "/f"])
    else:
        print("  unsupported platform for scheduler; skipping")

    step("extensions")
    for target in agents.EXTENSION_TARGETS:
        ext_dir = target["extension_dir"](os.environ)
        remove_symlink_or_move(os.path.join(ext_dir, "sleep"), "%s extension" % target["key"])

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