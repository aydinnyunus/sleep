"""End-to-end tests for the sleep CLI: every assertion crosses a process boundary.

Nothing here imports the product. sleep.py, hooks/*.sh, tools/wire.py, install.sh
and uninstall.sh are run as subprocesses against a throwaway $SLEEP_HOME / $HOME /
$CLAUDE_HOME / $OMO_HOME, and each claim is checked on the filesystem those
processes left behind.

Two guarantees are enforced by the fixture rather than by care:

  * No model is ever called. Every child gets a PATH whose first entry is a
    sandbox bin holding a poison `claude` that appends a line to a marker file,
    and tearDown fails if that marker was ever written. The same bin holds a
    `launchctl` that exits 1, which keeps uninstall.sh's nightly-job branch from
    reaching the real machine.
  * Nothing outside the sandbox is written. install/uninstall run with HOME,
    CLAUDE_HOME, OMO_HOME and SLEEP_HOME inside the temp dir, and one test pins
    the real ~/.claude-work/settings.json by content hash across an install.

Clocks: sleepkit/clock.py does not exist in this repo and paths.now() has no
injection seam, so no test freezes a clock. Anything that needed "older than"
either uses a threshold that is unambiguously true for a fresh file, or writes
state.json directly, which is plain data rather than a test-only product hook.

Run: python3 tests/test_cli.py
"""
import glob
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLEEPCTL = os.path.join(REPO, "sleep.py")
WIRE = os.path.join(REPO, "tools", "wire.py")
INSTALL = os.path.join(REPO, "install.sh")
UNINSTALL = os.path.join(REPO, "uninstall.sh")
HOOKS = os.path.join(REPO, "hooks")
PY = sys.executable
REAL_CLAUDE_SETTINGS = os.path.expanduser("~/.claude-work/settings.json")

# tools/wire.py WIRING, restated here so the test fails when the product silently
# moves an event to another profile. The pi/omo profile must gain nothing at all:
# install.sh only ever CLEARS an omo entry, because that engine runs none of these
# Claude-shaped command hooks.
SCRIPTS = {
    "UserPromptSubmit": "sleep-capture.sh",
    "Stop": "sleep-consolidate.sh",
    "PreCompact": "sleep-consolidate.sh",
    "PostToolUse": "sleep-posttool.sh",
}
EVENTS = sorted(SCRIPTS)


def sleep_command(event):
    return os.path.join(REPO, "hooks", SCRIPTS[event])


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class Sandbox(unittest.TestCase):
    """One throwaway filesystem plus one sanitized environment, per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sleepcli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.mk("home")
        self.sleep_home = os.path.join(self.home, ".sleep")
        self.claude_home = os.path.join(self.home, ".claude-work")
        self.omo_home = os.path.join(self.home, ".omo", "agent")
        self.memory = self.mk("memory-repo")
        self.proj_a = self.mk("work/project-a")
        self.proj_b = self.mk("work/project-b")
        self.bin = self.mk("sandbox-bin")
        self.marker = self.at("model-was-called")
        self._make_script("claude",
                          '#!/bin/sh\nprintf "called: $*\\n" >> "$SLEEP_TEST_MODEL_MARKER"\n'
                          'exit 99\n')
        self._make_script("launchctl", "#!/bin/sh\nexit 1\n")
        os.symlink(PY, os.path.join(self.bin, "python3"))

    def tearDown(self):
        if os.path.exists(self.marker):
            self.fail("a child process reached a model:\n" + self.read(self.marker))

    # --------------------------------------------------------------- sandbox --
    def mk(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def at(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def read_json(self, path):
        return json.loads(self.read(path))

    def write_json(self, path, doc):
        return self.write(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")

    def _make_script(self, name, text):
        path = os.path.join(self.bin, name)
        self.write(path, text)
        os.chmod(path, 0o755)

    def env(self, **overrides):
        env = dict(os.environ)
        for leaked in ("SLEEP_DISABLE", "SLEEP_SYNC", "SLEEPCTL", "SLEEP_MIN_RECORDS",
                       "SLEEP_MIN_INTERVAL", "SLEEP_MODEL", "SLEEP_SESSION",
                       "SLEEP_MEMORY_DIR", "SLEEP_HOME", "SLEEP_TEST_MODEL_MARKER",
                       "CLAUDE_HOME", "OMO_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_BIN"):
            env.pop(leaked, None)
        env.update({
            "HOME": self.home,
            "SLEEP_HOME": self.sleep_home,
            "CLAUDE_HOME": self.claude_home,
            "OMO_HOME": self.omo_home,
            "CLAUDE_CONFIG_DIR": self.claude_home,
            "SLEEP_MEMORY_DIR": self.memory,
            "CLAUDE_BIN": os.path.join(self.bin, "absent-claude"),
            "SLEEP_TEST_MODEL_MARKER": self.marker,
            "PATH": os.pathsep.join([self.bin, os.path.dirname(PY), "/bin", "/usr/bin"]),
        })
        env.update(overrides)
        return env

    # ---------------------------------------------------------------- runners --
    def cli(self, *args, **kw):
        return self._run([PY, SLEEPCTL] + list(args), stdin=kw.pop("stdin", ""),
                         cwd=kw.pop("cwd", self.proj_a), env=self.env(**kw))

    def bash(self, script, *args, **kw):
        return self._run(["/bin/bash", script] + list(args), stdin=kw.pop("stdin", ""),
                         cwd=kw.pop("cwd", self.tmp), env=self.env(**kw))

    def python(self, script, *args, **kw):
        return self._run([PY, script] + list(args), stdin=kw.pop("stdin", ""),
                         cwd=kw.pop("cwd", REPO), env=self.env(**kw))

    def _run(self, argv, stdin, cwd, env):
        return subprocess.run(argv, cwd=cwd, env=env, input=stdin,
                              capture_output=True, text=True, timeout=30)

    # --------------------------------------------------------------- fixtures --
    def seed_candidate(self, cid, body, source="user", trust="verified", kind="fact",
                       status="provisional", cwd=None, session="seed"):
        """Write one T1 file by hand.

        Candidates are normally produced by `distill`, and distill is a model call,
        so the CLI boundary can only be exercised against pre-existing T1 files. The
        frontmatter is spelled out here rather than borrowed from store.emit(), so
        the fixture pins the on-disk contract instead of quietly following the
        product if it ever changes the format.
        """
        meta = [("id", cid), ("ts", "2026-01-01T00:00:00+0300"), ("source", source),
                ("trust", trust), ("kind", kind), ("status", status),
                ("session", session), ("cwd", cwd or self.proj_a), ("tags", ""),
                ("evidence", ""), ("supersedes", ""), ("superseded_by", ""),
                ("redacted", "false"), ("notes", "")]
        text = "---\n" + "".join("%s: %s\n" % pair for pair in meta) + "---\n" + body + "\n"
        return self.write(os.path.join(self.sleep_home, "candidates", cid + ".md"), text)

    def buffer_dir(self):
        return os.path.join(self.sleep_home, "buffer")

    def buffer_names(self):
        d = self.buffer_dir()
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def buffer_records(self, lenient=False):
        """Every record in the T0 buffer. lenient mirrors read_buffer's skip."""
        out = []
        for name in self.buffer_names():
            for line in self.read(os.path.join(self.buffer_dir(), name)).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    if not lenient:
                        raise
        return out

    def quarantined(self):
        d = os.path.join(self.sleep_home, ".quarantine")
        if not os.path.isdir(d):
            return []
        return [self.read_json(os.path.join(d, n)) for n in sorted(os.listdir(d))]

    def tree_state(self, root):
        """relpath -> content hash for every file under root, plus dir names."""
        state = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            here = os.path.relpath(dirpath, root)
            if here != ".":
                state[here + "/"] = "dir"
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                state[os.path.relpath(path, root)] = "link" if os.path.islink(path) else digest(path)
        return state

    def hook_commands(self, settings_path):
        doc = self.read_json(settings_path)
        return [(event, hook.get("command"))
                for event, entries in (doc.get("hooks") or {}).items()
                for entry in entries for hook in entry.get("hooks", [])]

    def foreign_entries(self, settings_path):
        """Hook entries that are not the product's own, keyed by event."""
        doc = self.read_json(settings_path)
        out = {}
        for event, entries in (doc.get("hooks") or {}).items():
            kept = [e for e in entries
                    if not any(str(h.get("command", "")).startswith(REPO)
                               for h in e.get("hooks", []))]
            if kept:
                out[event] = kept
        return out


class T0Capture(Sandbox):
    """capture: what is allowed into memory, and what a refused write leaves."""

    def test_capture_writes_one_record_with_agent_provenance(self):
        got = self.cli("capture", "curl timeouts need 90s on the internal proxy",
                       "--session", "cap1", "--cwd", self.proj_a, "--tags", "proxy,ci")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertTrue(got.stdout.startswith("OK b"), got.stdout)
        self.assertEqual(self.buffer_names(), ["cap1.jsonl"])
        recs = self.buffer_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["id"], got.stdout.split()[1])
        self.assertEqual(recs[0]["source"], "agent")
        self.assertEqual(recs[0]["kind"], "fact")
        self.assertEqual(recs[0]["cwd"], self.proj_a)
        self.assertEqual(recs[0]["session"], "cap1")
        self.assertEqual(recs[0]["tags"], "proxy|ci")
        self.assertEqual(recs[0]["redacted"], "false")

    def test_capture_accepts_stdin_and_refuses_nothing_at_all(self):
        got = self.cli("capture", "--session", "cap2", stdin="  piped observation  ")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(self.buffer_records()[0]["text"], "piped observation")
        empty = self.cli("capture", "--session", "cap2", stdin="   \n")
        self.assertEqual(empty.returncode, 3)
        self.assertIn("nothing to capture", empty.stdout)
        self.assertEqual(len(self.buffer_records()), 1)

    def test_capture_validates_kind_and_source_at_the_boundary(self):
        for flag in (("--kind", "rumor"), ("--source", "model")):
            got = self.cli("capture", "--text", "some text", *flag)
            self.assertEqual(got.returncode, 2, flag)
            self.assertIn("invalid choice", got.stderr)
        self.assertEqual(self.buffer_names(), [])

    def test_the_cli_can_mint_source_user_too_not_only_the_hook(self):
        """Asserted as-is, because it contradicts the documented contract.

        hooks/sleep-capture.sh says it is the only path that may produce source=user
        records. The CLI disagrees: --source user is an accepted choice, so any
        process that can run sleep.py can claim the user's provenance. Reported under
        PRODUCTION BUGS; the suite pins current behavior so the change is loud.
        """
        got = self.cli("capture", "--source", "user", "always use bun test, never npm",
                       "--session", "cap3")
        self.assertEqual(got.returncode, 0, got.stdout)
        self.assertEqual(self.buffer_records()[0]["source"], "user")
        self.assertEqual(self.quarantined(), [])

    def test_a_directive_from_a_non_user_source_is_quarantined_not_buffered(self):
        got = self.cli("capture", "--source", "agent",
                       "always run migrations before starting the api", "--session", "cap4")
        self.assertEqual(got.returncode, 3)
        self.assertIn("quarantined", got.stdout)
        self.assertIn("needs source=user", got.stdout)
        self.assertEqual(self.buffer_names(), [], "a refused capture must leave no T0 record")
        parked = self.quarantined()
        self.assertEqual(len(parked), 1)
        self.assertIn("directive-shaped", parked[0]["reason"])

    def test_a_secret_is_redacted_before_it_reaches_the_buffer_file(self):
        secret = "sk-" + "a" * 24
        got = self.cli("capture", "--source", "user",
                       "rotate the deploy key " + secret, "--session", "cap5")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        raw = self.read(os.path.join(self.buffer_dir(), "cap5.jsonl"))
        self.assertNotIn(secret, raw, "the credential must not exist on disk anywhere")
        self.assertIn("<redacted:credential>", raw)
        self.assertEqual(self.buffer_records()[0]["redacted"], "true")

    def test_project_scope_rides_on_cwd_because_capture_has_no_project_flag(self):
        got = self.cli("capture", "--text", "scoped note", "--project", self.proj_a)
        self.assertEqual(got.returncode, 2)
        self.assertIn("unrecognized arguments: --project", got.stderr)
        self.assertEqual(self.buffer_names(), [])
        self.cli("capture", "--text", "scoped note", "--cwd", self.proj_a, "--session", "cap6")
        self.assertEqual(self.buffer_records()[0]["cwd"], self.proj_a)

    def test_prune_has_no_dry_run_and_the_real_prune_archives_instead_of_deleting(self):
        self.cli("capture", "--text", "note to archive", "--session", "cap7")
        source = os.path.join(self.buffer_dir(), "cap7.jsonl")
        payload = self.read(source)
        dry = self.cli("prune", "--dry-run", "--session", "cap7")
        self.assertEqual(dry.returncode, 2, "prune exposes no --dry-run at all")
        self.assertTrue(os.path.isfile(source))
        got = self.cli("prune", "--session", "cap7")
        self.assertEqual(got.returncode, 0, got.stderr)
        report = json.loads(got.stdout)
        self.assertEqual(len(report["moved"]), 1)
        self.assertTrue(os.path.isfile(report["moved"][0]), "prune moves, it never deletes")
        self.assertEqual(self.read(report["moved"][0]), payload)
        self.assertFalse(os.path.exists(source))
        self.assertEqual(report["remaining"]["records"], 0)

    def test_prune_with_an_older_than_threshold_spares_a_fresh_file(self):
        self.cli("capture", "--text", "just written", "--session", "cap8")
        got = self.cli("prune", "--older-than", "1", "--session", "cap8")
        self.assertEqual(json.loads(got.stdout)["moved"], [])
        self.assertTrue(os.path.isfile(os.path.join(self.buffer_dir(), "cap8.jsonl")))


class StatusShowInject(Sandbox):
    """status, show and inject: the read side, which must never cost a model call."""

    def seed_pool(self):
        self.cli("capture", "--text", "first observation", "--session", "st")
        self.cli("capture", "--text", "second observation", "--session", "st")
        self.cli("capture", "--source", "agent", "never log the gitlab token",
                 "--session", "st")
        self.seed_candidate("calpha", "The internal gitlab needs a PAT with read_api scope.",
                            cwd=self.proj_a)
        self.seed_candidate("czulu", "Mars dashboards lag by about two minutes.",
                            source="agent", trust="observed", cwd=self.proj_b)

    def test_status_counts_all_three_tiers(self):
        self.seed_pool()
        got = self.cli("status")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertRegex(got.stdout, r"T0 buffer\s+2 records / 1 files / \d+ bytes")
        self.assertRegex(got.stdout, r"T1 candidates\s+2 provisional, 2 total")
        self.assertRegex(got.stdout, r"quarantine\s+1 parked")
        self.assertIn("T2 canonical   " + self.memory, got.stdout)
        self.assertIn("provisional (context only, never directives):", got.stdout)
        listing = got.stdout.split("provisional (context only, never directives):")[1]
        self.assertIn("calpha", listing)
        self.assertIn("czulu", listing)
        self.assertIn("user", listing)
        self.assertIn("observed", listing)
        self.assertIn("read_api", listing,
                      "the provisional listing must show the body, not just id/source/trust "
                      "(fixed: cmd_status now passes with_body=True)")

    def test_status_rejects_json_and_verbose_needs_no_model(self):
        self.seed_pool()
        self.assertEqual(self.cli("status", "--json").returncode, 2)
        self.seed_candidate("ctwin", "Mars dashboards lag by about two minutes.",
                            source="agent", trust="observed", cwd=self.proj_b)
        verbose = self.cli("status", "--verbose")
        self.assertEqual(verbose.returncode, 0, verbose.stderr)
        self.assertIn("doctor: ", verbose.stdout)
        self.assertIn("near-duplicate-candidates", verbose.stdout)
        self.assertFalse(os.path.exists(self.marker), "--verbose is a read, not a sleep")

    def test_status_survives_a_malformed_buffer_line(self):
        self.cli("capture", "--text", "readable record", "--session", "mg")
        with open(os.path.join(self.buffer_dir(), "mg.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("{not json at all\n")
        got = self.cli("status")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertRegex(got.stdout, r"T0 buffer\s+1 records")
        self.assertEqual(len(self.buffer_records(lenient=True)), 1)

    def test_show_prints_the_record_with_its_provenance(self):
        self.seed_candidate("cshow", "Use rtk proxy for raw command passthrough.",
                            cwd=self.proj_a)
        got = self.cli("show", "cshow")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("id: cshow", got.stdout)
        self.assertIn("source: user", got.stdout)
        self.assertIn("trust: verified", got.stdout)
        self.assertIn("cwd: " + self.proj_a, got.stdout)
        self.assertIn("# rendered for context:", got.stdout)
        self.assertIn("[user-confirmed] Use rtk proxy", got.stdout)
        missing = self.cli("show", "cnotthere")
        self.assertEqual(missing.returncode, 2, "show uses 2 where promote uses 3")
        self.assertIn("no such candidate", missing.stdout)

    def test_inject_returns_only_the_matching_project(self):
        self.seed_pool()
        got = self.cli("inject", "--project", self.proj_a)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn('<provisional-memory unverified="true">', got.stdout)
        self.assertIn("</provisional-memory>", got.stdout)
        self.assertIn("read_api", got.stdout)
        self.assertNotIn("Mars dashboards", got.stdout)

    def test_inject_without_a_project_returns_every_provisional_record(self):
        self.seed_pool()
        got = self.cli("inject")
        self.assertIn("read_api", got.stdout)
        self.assertIn("Mars dashboards", got.stdout)

    def test_inject_for_a_project_with_no_t1_prints_nothing_and_exits_zero(self):
        self.seed_pool()
        empty = self.mk("work/project-empty")
        got = self.cli("inject", "--project", empty)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(got.stdout, "", "no block, not an empty wrapper")

    def test_inject_max_caps_the_block_but_keeps_the_frame(self):
        self.seed_candidate("cmax1", "First provisional line for the max test.",
                            cwd=self.proj_a)
        self.seed_candidate("cmax2", "Second provisional line for the max test.",
                            cwd=self.proj_a)
        got = self.cli("inject", "--project", self.proj_a, "--max", "1")
        self.assertIn("First provisional line", got.stdout)
        self.assertNotIn("Second provisional line", got.stdout)
        self.assertIn("</provisional-memory>", got.stdout)

    def test_inject_frames_unverified_records_as_context_and_has_no_json_flag(self):
        self.seed_candidate("cinferred", "The ci flake may be a runner cache miss.",
                            source="inferred", trust="speculative", cwd=self.proj_a)
        got = self.cli("inject", "--project", self.proj_a)
        self.assertIn("[unverified memory, source=inferred trust=speculative", got.stdout)
        self.assertIn("not instructions", got.stdout)
        self.assertNotIn("[user-confirmed]", got.stdout)
        self.assertEqual(self.cli("inject", "--json").returncode, 2)


class Gates(Sandbox):
    """The two human gates plus the sleep policy gate, on their no-cost paths only."""

    def fill(self, count, session="gate"):
        for i in range(count):
            self.cli("capture", "--text", "observation number %d for the gate" % i,
                     "--session", session)

    def test_sleep_dry_run_reports_a_thin_buffer_and_writes_nothing(self):
        self.fill(2)
        before = self.tree_state(self.sleep_home)
        got = self.cli("sleep", "--dry-run")
        self.assertEqual(got.returncode, 0, got.stderr)
        report = json.loads(got.stdout)
        self.assertEqual(report["action"], "skipped")
        self.assertEqual(report["buffer_records"], 2)
        self.assertIn("needs 3", report["reason"])
        self.assertEqual(self.tree_state(self.sleep_home), before)

    def test_sleep_dry_run_says_would_run_and_consumes_no_buffer(self):
        self.fill(3)
        before = self.tree_state(self.sleep_home)
        got = self.cli("sleep", "--dry-run")
        self.assertEqual(got.returncode, 0, got.stderr)
        report = json.loads(got.stdout)
        self.assertEqual(report["action"], "would-run", report)
        self.assertEqual(report["reason"], "policy satisfied")
        self.assertEqual(len(self.buffer_records()), 3, "a dry run must not prune the buffer")
        self.assertEqual(self.tree_state(self.sleep_home), before,
                         "a dry run must not take the lock or write state.json")
        self.assertFalse(os.path.exists(self.marker), "would-run must never reach llm.run")

    def test_sleep_honours_the_minimum_interval_and_force_overrides_the_record_gate(self):
        self.fill(4)
        self.write_json(os.path.join(self.sleep_home, "state.json"),
                        {"last_sleep": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        soon = json.loads(self.cli("sleep", "--dry-run", "--min-records", "1").stdout)
        self.assertEqual(soon["action"], "skipped")
        self.assertIn("minimum interval 6h", soon["reason"])
        forced = json.loads(self.cli("sleep", "--dry-run", "--force",
                                     "--min-records", "99").stdout)
        self.assertEqual(forced["action"], "would-run")
        self.assertIn("(forced)", forced["reason"])

    def test_distill_reports_an_empty_buffer_and_a_dry_run_sends_nothing(self):
        empty = self.cli("distill", "--dry-run")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(json.loads(empty.stdout)["reason"], "buffer empty")
        self.fill(2)
        dry = self.cli("distill", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        report = json.loads(dry.stdout)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["buffer_records"], 2)
        self.assertIn("observation number 0 for the gate", report["would_send"])
        self.assertFalse(os.path.exists(self.marker),
                         "distill --dry-run must stop before the model call")

    def test_promote_dry_run_moves_nothing(self):
        self.cli("status")
        self.seed_candidate("cdry", "Nightly builds need the vault rotation first.",
                            cwd=self.proj_a)
        self.write(os.path.join(self.memory, "MEMORY.md"), "# Index\n- hand written line\n")
        before_mem = self.tree_state(self.memory)
        before_sleep = self.tree_state(self.sleep_home)
        got = self.cli("promote", "cdry", "--memory-dir", self.memory, "--dry-run")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("would write", got.stdout)
        self.assertEqual(self.tree_state(self.memory), before_mem)
        self.assertEqual(self.tree_state(self.sleep_home), before_sleep)

    def test_promoted_file_carries_the_text_not_only_frontmatter(self):
        """The T2 file must hold the memory itself.

        `emit(record, body)` takes the body as an argument, and promote called it
        with one argument: the store received a 245-byte frontmatter husk whose
        only trace of the fact was the truncated index line in MEMORY.md.
        """
        text = ("Rtk gain reports token savings, and the binary must be resolved "
                "with `which rtk` first because Rust Type Kit uses the same name.")
        self.write(os.path.join(self.memory, "MEMORY.md"), "# Index\n")
        self.seed_candidate("cbody", text, cwd=self.proj_a)
        got = self.cli("promote", "cbody", "--memory-dir", self.memory)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        written = [f for f in os.listdir(self.memory) if f.endswith(".md") and f != "MEMORY.md"]
        self.assertEqual(len(written), 1, written)
        body_file = self.read(os.path.join(self.memory, written[0]))
        self.assertIn(text, body_file, "the promoted file must contain the full text")
        self.assertGreater(len(body_file), len(text) + 40, "not a frontmatter husk")
        self.assertIn("status: promoted", body_file,
                      "a T2 record must not claim to be provisional")
        self.assertNotIn("status: provisional", body_file)
        # the index is rewritten, so the previous copy has to survive somewhere
        bdir = os.path.join(self.sleep_home, "archive", "memory")
        backups = [f for f in os.listdir(bdir) if f.startswith("MEMORY.md.bak-")] if os.path.isdir(bdir) else []
        self.assertEqual(len(backups), 1, backups)
        self.assertIn("# Index", self.read(os.path.join(bdir, backups[0])))

    def test_promote_an_unknown_id_fails_and_leaves_state_intact(self):
        self.cli("status")
        self.seed_candidate("ckeep", "Any provisional record at all.", cwd=self.proj_a)
        before_mem = self.tree_state(self.memory)
        before_sleep = self.tree_state(self.sleep_home)
        got = self.cli("promote", "cdoesnotexist", "--memory-dir", self.memory)
        self.assertEqual(got.returncode, 3)
        self.assertIn("no such candidate", got.stdout)
        self.assertEqual(self.tree_state(self.memory), before_mem)
        self.assertEqual(self.tree_state(self.sleep_home), before_sleep)

    def test_promote_is_expansion_only_idempotent_and_keeps_the_index_intact(self):
        hand = "- **hand-written**: this line must survive byte for byte\n"
        self.write(os.path.join(self.memory, "MEMORY.md"), "# Index\n\n" + hand)
        self.seed_candidate("cgood", "Rtk gain reports token savings; check which rtk first.",
                            cwd=self.proj_a)
        got = self.cli("promote", "cgood", "--memory-dir", self.memory)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("PROMOTED wrote", got.stdout)
        promoted = [f for f in os.listdir(self.memory) if f.endswith(".md") and f != "MEMORY.md"]
        self.assertEqual(len(promoted), 1, promoted)
        index = self.read(os.path.join(self.memory, "MEMORY.md"))
        self.assertIn(hand, index, "the managed block must be appended, never rewritten")
        self.assertIn("<!-- sleep:begin", index)
        self.assertIn("<!-- sleep:end -->", index)
        self.assertEqual(index.count("Rtk gain reports token savings"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.sleep_home, "candidates", "cgood.md")))
        archived = os.path.join(self.sleep_home, "archive", "candidates", "cgood.md")
        self.assertIn("status: promoted", self.read(archived))
        again = self.cli("promote", "cgood", "--memory-dir", self.memory)
        self.assertEqual(again.returncode, 3)
        self.assertIn("already promoted", again.stdout)
        self.assertEqual([f for f in os.listdir(self.memory)
                          if f.endswith(".md") and f != "MEMORY.md"], promoted,
                         "a second promote must not add a duplicate T2 entry")
        self.assertEqual(self.read(os.path.join(self.memory, "MEMORY.md"))
                         .count("Rtk gain reports token savings"), 1)

    def test_refuse_records_the_verdict_and_keeps_the_record_out_of_t2(self):
        self.seed_candidate("cbad", "A fact that turned out to be wrong.", cwd=self.proj_a)
        before = sorted(os.listdir(self.memory))
        got = self.cli("refuse", "cbad", "--note", "contradicted by measurement")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("refused cbad", got.stdout)
        text = self.read(os.path.join(self.sleep_home, "candidates", "cbad.md"))
        self.assertIn("status: refused", text)
        self.assertIn("contradicted by measurement", text)
        self.assertEqual(sorted(os.listdir(self.memory)), before,
                         "refuse is a verdict on T1, it must not touch T2")
        injected = self.cli("inject", "--project", self.proj_a)
        self.assertNotIn("A fact that turned out", injected.stdout,
                         "a refused record must stop being injected")
        self.assertEqual(self.cli("refuse", "cnope").returncode, 3)

    def test_supersede_links_both_ends_of_the_pair(self):
        self.seed_candidate("cold", "Old note about the proxy timeout.", cwd=self.proj_a)
        self.seed_candidate("cnew", "New note about the proxy timeout window.", cwd=self.proj_a)
        got = self.cli("supersede", "cold", "cnew")
        self.assertEqual(got.returncode, 0, got.stderr)
        old = self.read(os.path.join(self.sleep_home, "candidates", "cold.md"))
        new = self.read(os.path.join(self.sleep_home, "candidates", "cnew.md"))
        self.assertIn("status: superseded", old)
        self.assertIn("superseded_by: cnew", old)
        self.assertIn("supersedes: cold", new)


class ProvenanceCeiling(Sandbox):
    """The ratchet: nothing inferred may arrive in T2 wearing the user's voice."""

    def test_an_inferred_speculative_record_cannot_reach_t2(self):
        got = self.cli("capture", "--source", "inferred",
                       "the ci flake looks like a runner cache miss", "--session", "prov")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertEqual(self.buffer_records()[0]["source"], "inferred")
        self.seed_candidate("cinferred", "The ci flake looks like a runner cache miss.",
                            source="inferred", trust="speculative", cwd=self.proj_a)
        before = sorted(os.listdir(self.memory))
        promoted = self.cli("promote", "cinferred", "--memory-dir", self.memory)
        self.assertEqual(promoted.returncode, 3)
        self.assertIn("refusing to persist speculative memory from source=inferred",
                      promoted.stdout)
        self.assertEqual(sorted(os.listdir(self.memory)), before,
                         "a refused promote must leave T2 empty")
        self.assertEqual(self.read(os.path.join(self.sleep_home, "candidates",
                                                "cinferred.md")).count("status: provisional"), 1)

    def test_an_inferred_record_can_never_be_rendered_as_user_confirmed(self):
        """Even a T1 file that claims a louder trust must render as background.

        render_for_context needs source=user AND trust=verified together, so the
        phrase a model sees cannot be lifted by the trust field alone. Note this is
        the render side only: promote's own guard reads the same trust field it is
        about to persist, which is the hole reported under PRODUCTION BUGS.
        """
        self.seed_candidate("cforged", "Text an agent inferred, labelled as the user's.",
                            source="inferred", trust="verified", cwd=self.proj_a)
        shown = self.cli("show", "cforged")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("[unverified memory, source=inferred trust=verified", shown.stdout)
        self.assertNotIn("[user-confirmed]", shown.stdout)
        injected = self.cli("inject", "--project", self.proj_a)
        self.assertNotIn("[user-confirmed]", injected.stdout)


class Doctor(Sandbox):
    """doctor: the read-only hygiene report, on a clean and a deliberately dirty tree."""

    def test_doctor_is_clean_on_a_fresh_sandbox_and_reads_only(self):
        self.cli("status")  # any run lays down the tier skeleton; then hold it still
        fresh = self.tree_state(self.sleep_home)
        mem_fresh = self.tree_state(self.memory)
        got = self.cli("doctor")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout, "clean\n")
        self.assertEqual(json.loads(self.cli("doctor", "--json").stdout), [])
        self.assertEqual(self.tree_state(self.sleep_home), fresh)
        self.assertEqual(self.tree_state(self.memory), mem_fresh)

    def test_doctor_reports_corruption_and_still_exits_zero(self):
        self.write(os.path.join(self.memory, "note.md"),
                   "# Note\n\n## Related\n\none\n\n## Related\n\ntwo\n")
        self.seed_candidate("cfirst", "Mars dashboards lag by about two minutes in staging.",
                            cwd=self.proj_a)
        self.seed_candidate("csecond", "Mars dashboards lag by about two minutes in staging.",
                            source="agent", trust="observed", cwd=self.proj_a)
        before = self.tree_state(self.memory)
        got = self.cli("doctor")
        self.assertIn("duplicate-heading-in-file (1)", got.stdout)
        self.assertIn("near-duplicate-candidates", got.stdout)
        self.assertEqual(got.returncode, 0,
                         "doctor reports, it never signals through the exit code")
        issues = json.loads(self.cli("doctor", "--json").stdout)
        kinds = sorted(i["issue"] for i in issues)
        self.assertIn("duplicate-heading-in-file", kinds)
        self.assertIn("near-duplicate-candidates", kinds)
        self.assertTrue(all("issue" in i for i in issues))
        self.assertEqual(self.tree_state(self.memory), before, "doctor is read-only")

    def test_doctor_does_not_flag_a_malformed_buffer_line(self):
        """The report is about memory hygiene, not buffer parsing: pinned so nobody
        assumes a corrupted buffer will surface here. `status` is what tolerates it."""
        self.cli("capture", "--text", "one good line", "--session", "doc")
        with open(os.path.join(self.buffer_dir(), "doc.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("}{ broken\n")
        got = self.cli("doctor")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout, "clean\n")
        self.assertRegex(self.cli("status").stdout, r"T0 buffer\s+1 records")


class HookBoundary(Sandbox):
    """The hook scripts exactly as a harness runs them: payload on stdin, silence back."""

    def test_capture_hook_is_the_user_provenance_path(self):
        payload = json.dumps({"session_id": "h1", "cwd": self.proj_a,
                              "prompt": "never open a git commit without asking me"})
        off = self.bash(os.path.join(HOOKS, "sleep-capture.sh"), stdin=payload,
                        SLEEP_DISABLE="1")
        self.assertEqual(off.returncode, 0)
        self.assertEqual(self.buffer_names(), [], "the kill switch must write nothing")
        got = self.bash(os.path.join(HOOKS, "sleep-capture.sh"), stdin=payload)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout, "", "hook stdout can be injected into context")
        recs = self.buffer_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["source"], "user")
        self.assertEqual(recs[0]["session"], "h1")
        self.assertEqual(recs[0]["cwd"], self.proj_a)
        self.assertEqual(recs[0]["kind"], "preference")
        self.assertEqual(recs[0]["tags"], "hook:userprompt")

    def test_posttool_hook_records_a_tool_trace_as_agent_provenance(self):
        self.cli("status")
        touched = os.path.join(self.proj_b, "app.py")
        payload = json.dumps({"session_id": "h2", "cwd": self.proj_b, "tool_name": "Read",
                              "tool_input": {"file_path": touched}})
        got = self.bash(os.path.join(HOOKS, "sleep-posttool.sh"), stdin=payload)
        self.assertEqual(got.returncode, 0, got.stderr)
        rec = self.buffer_records()[-1]
        self.assertEqual(rec["source"], "agent", "a tool event is never the user's own words")
        self.assertEqual(rec["tags"], "hook:posttool")
        self.assertEqual(rec["evidence"], touched)
        self.assertEqual(rec["cwd"], self.proj_b)

    def test_a_pathless_tool_event_is_counted_but_never_captured(self):
        self.cli("status")
        payload = json.dumps({"session_id": "h3", "cwd": self.proj_a, "tool_name": "bash",
                              "tool_input": {"command": "ls"}})
        got = self.bash(os.path.join(HOOKS, "sleep-posttool.sh"), stdin=payload)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.buffer_records(), [])
        log = self.read(os.path.join(self.sleep_home, "logs", "hook-fires.log"))
        self.assertIn("posttool", log)
        self.assertIn("bytes=", log)


class WireProfile(Sandbox):
    """tools/wire.py on its own: it is the only thing allowed to touch harness config."""

    def settings(self, doc):
        path = os.path.join(self.claude_home, "settings.json")
        self.write_json(path, doc)
        return path

    def wire(self, path, *extra):
        return self.python(WIRE, "--settings", path, *extra)

    def test_the_pi_profile_gains_no_wiring_at_all(self):
        path = self.settings({"hooks": {}})
        before = digest(path)
        got = self.wire(path, "--profile", "pi")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(digest(path), before,
                         "the engine runs no Claude-shaped command hooks")
        self.assertNotIn("wrote", got.stdout)
        self.assertEqual(glob.glob(path + ".bak-*"), [])

    def test_dry_run_lists_every_event_it_would_add_and_writes_nothing(self):
        path = self.settings({"hooks": {}})
        before = digest(path)
        got = self.wire(path, "--profile", "claude", "--dry-run")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("(dry-run) not written", got.stdout)
        for event in EVENTS:
            self.assertIn("%s: added %s" % (event, SCRIPTS[event]), got.stdout)
        self.assertEqual(digest(path), before)
        self.assertEqual(glob.glob(path + ".bak-*"), [])

    def test_an_unrecognized_entry_shape_is_left_alone_not_reshaped(self):
        weird = [{"matcher": "*", "notHooks": True}]
        path = self.settings({"hooks": {"Stop": weird}})
        got = self.wire(path, "--profile", "claude")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("unrecognized shape, left alone", got.stdout)
        self.assertEqual(self.read_json(path)["hooks"]["Stop"], weird)
        self.assertIn(sleep_command("PostToolUse"),
                      [cmd for _, cmd in self.hook_commands(path)])

    def test_removal_prunes_commands_not_entries_so_co_located_hooks_survive(self):
        """A bucket entry can hold several commands; removing ours must not take the
        others with it. The real ~/.omo/agent/hooks.json on this machine keeps
        ruff-lint, golangci-lint and a leftover sleep entry in one PostToolUse entry,
        so an entry-level removal would silently delete someone else's lint hook.
        """
        foreign = "/Users/example/.omo/agent/bin/ruff-lint-hook.sh"
        path = self.settings({"hooks": {"PostToolUse": [{"matcher": "", "hooks": [
            {"type": "command", "command": foreign, "timeout": 5},
            {"type": "command", "command": sleep_command("PostToolUse"), "timeout": 30},
        ]}]}})
        got = self.wire(path, "--profile", "all", "--remove")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("PostToolUse: removed", got.stdout)
        entries = self.read_json(path)["hooks"]["PostToolUse"]
        self.assertEqual([h["command"] for e in entries for h in e["hooks"]], [foreign])
        self.assertEqual(entries[0]["matcher"], "", "the entry's own keys must survive")

    def test_removal_drops_emptied_event_keys_instead_of_writing_empty_lists(self):
        path = self.settings({"hooks": {}})
        self.wire(path, "--profile", "claude")
        self.assertEqual(sorted(self.read_json(path)["hooks"]), EVENTS)
        removed = self.wire(path, "--profile", "claude", "--remove")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(self.read_json(path)["hooks"], {})
        self.assertNotIn("[]", self.read(path))


class InstallUninstall(Sandbox):
    """install.sh / uninstall.sh in a sandbox HOME that already holds foreign config."""

    FOREIGN_UP = [{"matcher": "", "hooks": [{"type": "command",
                                             "command": "/usr/bin/true", "timeout": 5}]}]
    FOREIGN_POST = [{"matcher": "*", "hooks": [{"type": "command",
                                                "command": "/usr/bin/false"}]}]
    FOREIGN_SESSION = [{"hooks": [{"type": "command",
                                   "command": "/usr/bin/echo foreign-session"}]}]
    OMO_FOREIGN = [{"matcher": "*", "hooks": [{"type": "command",
                                               "command": "/usr/bin/echo omo-foreign"}]}]

    def setUp(self):
        super().setUp()
        self.settings_path = os.path.join(self.claude_home, "settings.json")
        self.omo_hooks = os.path.join(self.omo_home, "hooks.json")
        self.write_json(self.settings_path, {
            "model": "a-model-i-chose",
            "permissions": {"allow": ["Bash(git status)"]},
            "hooks": {"UserPromptSubmit": self.FOREIGN_UP,
                      "PostToolUse": self.FOREIGN_POST,
                      "SessionStart": self.FOREIGN_SESSION},
        })
        legacy = {event: [{"hooks": [{"type": "command", "command": sleep_command(event),
                                      "timeout": 30}]}] for event in EVENTS}
        legacy["PostToolUse"] = list(self.OMO_FOREIGN) + legacy["PostToolUse"]
        self.write_json(self.omo_hooks, {"hooks": legacy, "extra_omo_key": {"keep": True}})
        self.settings_foreign_before = self.foreign_entries(self.settings_path)
        self.omo_foreign_before = self.foreign_entries(self.omo_hooks)
        self.settings_top_before = {k: v for k, v
                                    in self.read_json(self.settings_path).items()
                                    if k != "hooks"}

    def install(self, *flags):
        return self.bash(INSTALL, "--skip-tests", *flags)

    def uninstall(self, *flags):
        return self.bash(UNINSTALL, *flags)

    def skill_link(self):
        return os.path.join(self.claude_home, "skills", "sleep")

    def sleep_commands_in(self, path):
        return [cmd for _, cmd in self.hook_commands(path) if cmd.startswith(REPO)]

    def test_install_wires_only_the_claude_profile_including_posttooluse(self):
        real_before = digest(REAL_CLAUDE_SETTINGS) if os.path.isfile(REAL_CLAUDE_SETTINGS) else None
        got = self.install()
        self.assertEqual(got.returncode, 0, got.stdout[-1500:] + got.stderr[-1500:])
        doc = self.read_json(self.settings_path)
        self.assertEqual(sorted(doc["hooks"]), sorted(EVENTS + ["SessionStart"]))
        for event in EVENTS:
            self.assertIn(sleep_command(event), self.sleep_commands_in(self.settings_path),
                          "claude profile must carry %s" % event)
        self.assertIn(sleep_command("PostToolUse"),
                      [cmd for ev, cmd in self.hook_commands(self.settings_path)
                       if ev == "PostToolUse"])
        for event, cmd in self.hook_commands(self.settings_path):
            if cmd.startswith(REPO):
                entry = [e for e in doc["hooks"][event]
                         if any(h.get("command") == cmd for h in e.get("hooks", []))][0]
                self.assertEqual(entry["hooks"][0]["timeout"], 30)
        self.assertEqual(self.sleep_commands_in(self.omo_hooks), [],
                         "install must clear omo entries, the engine cannot run them")
        self.assertTrue(os.path.islink(self.skill_link()))
        self.assertEqual(os.path.realpath(self.skill_link()),
                         os.path.join(REPO, "skill", "sleep"))
        self.assertTrue(os.path.isdir(self.sleep_home))
        self.assertEqual(len(glob.glob(self.settings_path + ".bak-*")), 1)
        if real_before is not None:
            self.assertEqual(digest(REAL_CLAUDE_SETTINGS), real_before,
                             "an install in a sandbox must leave the real config alone")

    def test_install_keeps_foreign_entries_and_top_level_keys(self):
        self.install()
        doc = self.read_json(self.settings_path)
        self.assertEqual({k: v for k, v in doc.items() if k != "hooks"},
                         self.settings_top_before)
        self.assertEqual(self.foreign_entries(self.settings_path), self.settings_foreign_before)
        self.assertEqual(self.foreign_entries(self.omo_hooks), self.omo_foreign_before)
        self.assertEqual(self.read_json(self.omo_hooks)["extra_omo_key"], {"keep": True})

    def test_install_removes_a_legacy_sleep_entry_from_the_omo_hooks_file(self):
        seeded = self.sleep_commands_in(self.omo_hooks)
        self.assertEqual(len(seeded), len(EVENTS))
        self.install()
        self.assertEqual(self.sleep_commands_in(self.omo_hooks), [])
        omo = self.read_json(self.omo_hooks)
        self.assertEqual(omo["hooks"]["PostToolUse"], self.OMO_FOREIGN,
                         "the foreign omo entry must survive the cleanup")

    def test_a_second_install_changes_nothing(self):
        self.install()
        settings, omo = digest(self.settings_path), digest(self.omo_hooks)
        backups = len(glob.glob(self.settings_path + ".bak-*"))
        got = self.install()
        self.assertEqual(got.returncode, 0, got.stderr[-1500:])
        self.assertEqual(digest(self.settings_path), settings)
        self.assertEqual(digest(self.omo_hooks), omo)
        self.assertEqual(len(glob.glob(self.settings_path + ".bak-*")), backups,
                         "an idempotent install must not rewrite or re-back-up")

    def test_install_dry_run_and_an_unknown_flag_change_nothing(self):
        before_settings, before_omo = digest(self.settings_path), digest(self.omo_hooks)
        dry = self.install("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr[-1500:])
        self.assertIn("would:", dry.stdout)
        self.assertEqual(digest(self.settings_path), before_settings)
        self.assertEqual(digest(self.omo_hooks), before_omo)
        self.assertFalse(os.path.islink(self.skill_link()))
        self.assertFalse(os.path.isdir(self.sleep_home))
        bogus = self.bash(INSTALL, "--definitely-not-a-flag", "--skip-tests")
        self.assertEqual(bogus.returncode, 2)
        self.assertIn("unknown flag", bogus.stderr)
        self.assertEqual(digest(self.settings_path), before_settings)
        self.assertEqual(self.bash(INSTALL, "--help").returncode, 0)

    def test_uninstall_dry_run_changes_nothing(self):
        self.install()
        settings, omo = digest(self.settings_path), digest(self.omo_hooks)
        got = self.uninstall("--dry-run")
        self.assertEqual(got.returncode, 0, got.stdout[-1500:] + got.stderr[-1500:])
        self.assertIn("would:", got.stdout)
        self.assertEqual(digest(self.settings_path), settings)
        self.assertEqual(digest(self.omo_hooks), omo)
        self.assertTrue(os.path.islink(self.skill_link()))

    def test_uninstall_removes_exactly_the_sleep_entries(self):
        self.install()
        promoted_note = self.write(os.path.join(self.memory, "keep-me.md"), "# keep\n")
        got = self.uninstall()
        self.assertEqual(got.returncode, 0, got.stdout[-1500:] + got.stderr[-1500:])
        doc = self.read_json(self.settings_path)
        self.assertEqual(self.sleep_commands_in(self.settings_path), [])
        self.assertEqual(self.foreign_entries(self.settings_path), self.settings_foreign_before)
        self.assertEqual(sorted(doc["hooks"]), ["PostToolUse", "SessionStart", "UserPromptSubmit"],
                         "events emptied by removal must be dropped, not left as []")
        self.assertNotIn("[]", self.read(self.settings_path))
        self.assertEqual(doc["hooks"]["UserPromptSubmit"], self.FOREIGN_UP)
        self.assertEqual(doc["hooks"]["PostToolUse"], self.FOREIGN_POST)
        self.assertEqual(self.sleep_commands_in(self.omo_hooks), [])
        self.assertFalse(os.path.islink(self.skill_link()))
        self.assertTrue(os.path.isdir(self.sleep_home), "uninstall must not delete captured state")
        self.assertEqual(digest(promoted_note), digest(os.path.join(self.memory, "keep-me.md")))
        after = digest(self.settings_path)
        again = self.uninstall()
        self.assertEqual(again.returncode, 0, again.stderr[-1500:])
        self.assertEqual(digest(self.settings_path), after, "uninstall must be idempotent")


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
key = "SLEEP_FAKE_ON" if "provisional-memory" in prompt else "SLEEP_FAKE_OFF"
print(json.dumps({"type": "result", "num_turns": 3, "total_cost_usd": 0.01,
                  "duration_ms": 120, "session_id": "fake",
                  "result": os.environ[key]}))
"""


FAKE_CLAUDE_SILENT = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print("Not logged in. Please run /login")
print(json.dumps({"type": "result", "is_error": True, "num_turns": 1,
                  "total_cost_usd": 0.0, "duration_ms": 5, "session_id": "fake",
                  "result": "Not logged in. Please run /login"}))
sys.exit(1)
"""

FAKE_CLAUDE_PROSE = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "num_turns": 1, "total_cost_usd": 0.01,
                  "duration_ms": 50, "session_id": "fake",
                  "result": "Nothing worth carrying forward. The session was routine."}))
"""


class Unattended(Sandbox):
    """The replay and nightly path, driven the way launchd drives it.

    Every model call here goes to a fake claude on CLAUDE_BIN that answers from its
    stdin: it returns the ON answer only when the provisional-memory block actually
    reached the prompt. That makes the treatment itself observable, which is what the
    scorer used to assert without any evidence. The poison claude in the sandbox bin
    still fails the test if any code path goes looking for the real binary.
    """

    ON = "The ALB idle timeout is 300s while the stream runs 500s, so it is cut."
    OFF = "I need the access logs before I can say what dropped the connection."

    def fake_claude(self):
        self._make_script("fake-claude", FAKE_CLAUDE)
        return os.path.join(self.bin, "fake-claude")

    def tasks_file(self, measurable=True):
        task = {"id": "idle-timeout",
                "prompt": "Our long streaming calls die at 500 seconds. Why?",
                "criteria": "Names the ALB idle timeout as the cause."}
        if measurable:
            task["criteria_keywords"] = [["idle timeout", "idle_timeout"],
                                         ["alb", "load balancer"]]
        return self.write_json(self.at("tasks.json"), {"tasks": [task]})

    def test_replay_run_scores_the_treatment_it_claims(self):
        self.seed_candidate("c-mem-1",
                            "ALB idle timeout is 300s, shorter than the 500s Routify stream.")
        res = self.cli("replay", "--tasks", self.tasks_file(), "--limit", "1", "--run",
                       CLAUDE_BIN=self.fake_claude(),
                       SLEEP_FAKE_ON=self.ON, SLEEP_FAKE_OFF=self.OFF)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("treatment delivered: yes", res.stdout)
        self.assertIn("criteria miss/met", res.stdout)
        self.assertIn("helped", res.stdout)
        path = [line.split("report: ", 1)[1].strip()
                for line in res.stdout.splitlines() if line.startswith("report: ")]
        self.assertEqual(len(path), 1, res.stdout)
        report = self.read(path[0])
        self.assertIn("| idle-timeout | helped | miss/met |", report)
        self.assertIn("treatment delivered: yes", report)

    def test_replay_run_says_nothing_was_measured_without_memory(self):
        # Same fake, same task, no provisional record: the two arms then ran the same
        # prompt, so a report that claimed helped or hurt would be a fabrication.
        res = self.cli("replay", "--tasks", self.tasks_file(), "--limit", "1", "--run",
                       CLAUDE_BIN=self.fake_claude(),
                       SLEEP_FAKE_ON=self.ON, SLEEP_FAKE_OFF=self.OFF)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("treatment delivered: NO", res.stdout)
        self.assertIn("not measured: no provisional memory reached the on-runs", res.stdout)
        self.assertNotIn("helped", res.stdout)

    def test_replay_dry_run_flags_a_task_it_cannot_measure(self):
        res = self.cli("replay", "--tasks", self.tasks_file(measurable=False))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("NOT MEASURABLE", res.stdout)
        self.assertNotIn("NOT MEASURABLE",
                         self.cli("replay", "--tasks", self.tasks_file()).stdout)

    def fake_claude_named(self, name, script):
        self._make_script(name, script)
        return os.path.join(self.bin, name)

    def test_a_silent_model_does_not_spend_the_weekly_replay_clock(self):
        # Measured on this laptop: launchd hands the job no inherited token, every
        # sample exited 1, and the report came back as rows of "tie: both missed" with
        # the weekly clock already stamped. A pass where nothing answered is not a
        # measurement, and dating it as one hides the failure for a week.
        silent = self.fake_claude_named("fake-claude-silent", FAKE_CLAUDE_SILENT)
        res = self.cli("nightly", "--min-records", "99", "--replay-tasks",
                       self.tasks_file(), "--replay-limit", "1", "--replay-interval",
                       "168", CLAUDE_BIN=silent)
        doc = json.loads(res.stdout)
        self.assertTrue(doc["replay"].get("failed"), doc["replay"])
        self.assertIn("no model call answered", doc["replay"]["reason"])
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        state_path = os.path.join(self.sleep_home, "state.json")
        state = self.read_json(state_path) if os.path.exists(state_path) else {}
        self.assertNotIn("last_replay", state,
                         "a pass where nothing answered must not consume the interval")

    def test_a_failed_consolidation_is_not_reported_as_a_good_night(self):
        # launchd only reads the exit code. Returning 0 while the model call failed
        # means nobody ever looks, which is how a nightly that produced nothing for a
        # week would stay invisible.
        for text in ("first observation", "second observation", "third observation"):
            self.assertEqual(
                self.cli("capture", "--text", text, "--source", "user").returncode, 0)
        silent = self.fake_claude_named("fake-claude-silent", FAKE_CLAUDE_SILENT)
        res = self.cli("nightly", "--min-records", "2", "--min-interval", "0",
                       "--replay-interval", "168", CLAUDE_BIN=silent)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["sleep"]["action"], "ran", doc["sleep"])
        self.assertFalse(doc["sleep"]["ok"], doc["sleep"])
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)

    def test_a_night_with_nothing_to_keep_says_which_kind_of_night_it_was(self):
        # An empty night and a swallowed night used to look identical in the log. The
        # model answered cleanly here and produced no proposal-shaped block, so the
        # result must name that, while still reporting a successful night.
        for text in ("first observation", "second observation", "third observation"):
            self.assertEqual(
                self.cli("capture", "--text", text, "--source", "user").returncode, 0)
        prose = self.fake_claude_named("fake-claude-prose", FAKE_CLAUDE_PROSE)
        res = self.cli("nightly", "--min-records", "2", "--min-interval", "0",
                       "--replay-interval", "168", CLAUDE_BIN=prose)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["sleep"]["action"], "ran", doc["sleep"])
        self.assertEqual(doc["sleep"]["parse"]["mode"], "none", doc["sleep"])
        self.assertIn("no block matched the proposal contract", doc["sleep"]["warn"])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_nightly_reports_both_halves_and_never_promotes(self):
        for text in ("first observation", "second observation", "third observation"):
            self.assertEqual(
                self.cli("capture", "--text", text, "--source", "user").returncode, 0)
        t2_before = self.tree_state(self.memory)
        res = self.cli("nightly", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["sleep"]["action"], "would-run",
                         "the gate runs before the dry-run branch, so 3 buffered "
                         "records must reach would-run: %s" % doc["sleep"])
        self.assertTrue(doc["replay"]["skipped"])
        self.assertEqual(doc["replay"]["reason"], "dry run")
        self.assertEqual(self.tree_state(self.memory), t2_before,
                         "an unattended pass must not write to T2")

    def test_nightly_stamps_the_replay_clock_and_then_waits(self):
        args = ("nightly", "--min-records", "99", "--replay-tasks", self.tasks_file(),
                "--replay-limit", "1", "--replay-interval", "168")
        first = self.cli(*args, CLAUDE_BIN=self.fake_claude(),
                         SLEEP_FAKE_ON=self.ON, SLEEP_FAKE_OFF=self.OFF)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        doc = json.loads(first.stdout)
        self.assertEqual(doc["sleep"]["action"], "skipped",
                         "min-records 99 keeps the consolidation half out of this test")
        self.assertTrue(doc["replay"].get("ran"), doc["replay"])
        self.assertIn("verdict_tally", doc["replay"])
        state = self.read_json(os.path.join(self.sleep_home, "state.json"))
        self.assertIn("last_replay", state)
        second = self.cli(*args, CLAUDE_BIN=self.fake_claude(),
                          SLEEP_FAKE_ON=self.ON, SLEEP_FAKE_OFF=self.OFF)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        doc2 = json.loads(second.stdout)
        self.assertTrue(doc2["replay"].get("skipped"), doc2["replay"])
        self.assertIn("of 168.0 h", doc2["replay"]["reason"])
        zero = self.cli("nightly", "--min-records", "99", "--replay-tasks",
                        self.tasks_file(), "--replay-limit", "1",
                        "--replay-interval", "0", CLAUDE_BIN=self.fake_claude(),
                        SLEEP_FAKE_ON=self.ON, SLEEP_FAKE_OFF=self.OFF)
        self.assertTrue(json.loads(zero.stdout)["replay"].get("ran"),
                        "interval 0 is the escape hatch: every pass is due")

    def test_nightly_turns_a_missing_binary_into_json_not_a_traceback(self):
        # What an unattended 03:41 run looks like when the laptop has no claude
        # installed on this PATH. Both lookups have to be taken away: CLAUDE_BIN and
        # PATH, because binary() falls through from one to the other.
        res = self.cli("nightly", "--min-records", "99", "--replay-tasks",
                       self.tasks_file(), "--replay-limit", "1", "--replay-interval",
                       "168", PATH="/bin:/usr/bin")
        doc = json.loads(res.stdout)
        self.assertTrue(doc["replay"].get("failed"), doc["replay"])
        self.assertIn("FileNotFoundError", doc["replay"]["reason"])
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertNotIn("Traceback", res.stdout + res.stderr,
                         "an unattended failure must read as data in a log file")
        state_path = os.path.join(self.sleep_home, "state.json")
        state = self.read_json(state_path) if os.path.exists(state_path) else {}
        self.assertNotIn("last_replay", state,
                         "a failed pass must not consume the interval")


class NightlyJob(Sandbox):
    """The launchd step, tested as the shape of a job that runs with nobody watching.

    The rendered plist IS the behaviour at 03:41, and two real defects lived inside it.
    One: the installer never substituted the token this template used for the user's
    home, so every rendered job would have carried the literal "__HOME__/.local/bin" as
    PATH and lost both claude and python3 with it. Two: a prose comment here once
    contained "--min-interval", and XML 1.0 forbids "--" inside a comment: plutil -lint
    called that file OK and launchctl load accepted it into the live domain, while
    python's own parser rejected it at line 10. Both are pinned here, not in a comment.
    """

    def fake_root(self, broken=False):
        """A copy of the files install.sh reads, so a broken template can be tested
        without ever editing the repo's own plist."""
        root = tempfile.mkdtemp(prefix="sleep-root-", dir=self.tmp)
        for rel in ("install.sh", "uninstall.sh", "sleep.py"):
            shutil.copy2(os.path.join(REPO, rel), os.path.join(root, rel))
        for rel in ("launchd", "hooks", "tools", os.path.join("skill", "sleep")):
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copytree(os.path.join(REPO, rel), dst)
        if broken:
            path = os.path.join(root, "launchd", "com.aydinnyunus.sleep-nightly.plist")
            text = self.read(path).replace("min_interval 0 here", "--min-interval 0 here", 1)
            self.write(path, text)
        return root

    def test_the_rendered_job_parses_strictly_and_holds_no_placeholder(self):
        got = self.bash(INSTALL, "--skip-tests", "--launchd", SLEEP_SKIP_LOAD="1")
        self.assertEqual(got.returncode, 0, got.stdout[-1500:] + got.stderr[-1500:])
        self.assertIn("rendered and linted", got.stdout, got.stdout[-800:])
        doc = plistlib.loads(self.read(os.path.join(
            self.home, "Library", "LaunchAgents",
            "com.aydinnyunus.sleep-nightly.plist")).encode("utf-8"))
        argv = doc["ProgramArguments"]
        self.assertIn("nightly", argv, "the job must run the combined pass")
        self.assertIn("--replay-interval", argv)
        self.assertEqual(doc["StartCalendarInterval"], {"Hour": 3, "Minute": 41})
        self.assertEqual(doc["EnvironmentVariables"]["PATH"], self.home
                         + "/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
                         "the PATH a nightly run inherits is part of the product")
        self.assertEqual(doc["StandardOutPath"],
                         os.path.join(self.sleep_home, "logs", "nightly.log"))

    def test_a_plist_only_a_lenient_parser_accepts_is_refused(self):
        root = self.fake_root(broken=True)
        got = self.bash(os.path.join(root, "install.sh"), "--skip-tests", "--launchd",
                        SLEEP_SKIP_LOAD="1")
        self.assertIn("REFUSED", got.stdout, got.stdout[-800:])
        self.assertNotIn("rendered and linted", got.stdout,
                         "a plist that fails the strict lint must not reach launchctl")

if __name__ == "__main__":
    unittest.main(verbosity=2)
