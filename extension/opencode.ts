// @ts-nocheck
// Sleep capture adapter for OpenCode (opencode.ai). Layer 2b counterpart of the
// Pi/OmO extension, using OpenCode's TS plugin system.
//
// Contract (verified against a real installed plugin, ecc-hooks.ts):
//   .opencode/opencode.json:  { "plugin": ["./.opencode/plugins"] }
//   plugin file:              export default async ({ client, $, directory, worktree }) => ({...})
//   events:                   session.created, session.idle, tool.execute.after, file.edited
//
// There is no "user prompt submitted" event in OpenCode's public plugin set, so
// capture is implicit, mirroring Cursor: each completed tool call that carries a
// file path is written to the T0 buffer (source=agent), and session.idle triggers
// consolidation through the same policy gate the Pi extension uses (a model call
// is never blocking; runDetached is used). The Python adapter (sleepkit/hookio)
// parses the same Claude-shaped payload and decides provenance and trust.
//
// Nothing is written to stdout and no handler throws (this runs inside the agent
// loop). T2 is never written from here.
import { spawn, execFile } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, realpathSync, renameSync, statSync, openSync, closeSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const TRACE_LIMIT = 256 * 1024;
const DISABLED = () => process.env.SLEEP_DISABLE === "1";

const INSTALLED_REPO = "__SLEEP_REPO__";
const INSTALLED_PYTHON = "__SLEEP_PYTHON__";
const PYTHON = process.env.SLEEP_PYTHON
  || (INSTALLED_PYTHON.startsWith("__SLEEP_") ? "" : INSTALLED_PYTHON)
  || (process.platform === "win32" ? "py" : "python3");

function findRepo() {
  const self = (() => {
    try { return path.dirname(realpathSync(fileURLToPath(import.meta.url))); }
    catch { return ""; }
  })();
  const installed = INSTALLED_REPO.startsWith("__SLEEP_") ? "" : INSTALLED_REPO;
  const bases = [process.env.SLEEP_REPO, installed, self, path.join(self, "..")].filter(Boolean);
  for (const b of bases) {
    if (existsSync(path.join(b, "sleep.py"))) return path.resolve(b);
  }
  return path.resolve(bases[0] || ".");
}

const REPO = findRepo();
const SLEEPCTL = process.env.SLEEPCTL || path.join(REPO, "sleep.py");
const SLEEP_HOME = process.env.SLEEP_HOME || path.join(homedir(), ".sleep");
const LOG_DIR = path.join(SLEEP_HOME, "logs");
process.env.SLEEP_REPO ||= REPO; process.env.SLEEPCTL ||= SLEEPCTL; process.env.SLEEP_HOME ||= SLEEP_HOME;

function ts() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  const off = -d.getTimezoneOffset(), abs = Math.abs(off);
  return d.getFullYear() + "-" + p(d.getMonth()+1) + "-" + p(d.getDate()) + "T"
    + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds())
    + (off >= 0 ? "+" : "-") + p(Math.floor(abs/60)) + p(abs%60);
}

function note(kind, detail = "") {
  try {
    mkdirSync(LOG_DIR, { recursive: true });
    const file = path.join(LOG_DIR, "opencode.log");
    let st = null; try { st = statSync(file); } catch { /* first write */ }
    if (st && st.size > TRACE_LIMIT) renameSync(file, file + ".1");
    appendFileSync(file, ts() + "\t" + kind + "\t" + String(detail).slice(0, 200) + "\n");
  } catch { /* tracing must never break the session */ }
}

function sleepctl(args, timeoutMs = 20000) {
  if (DISABLED()) return Promise.resolve({ ok: false, reason: "sleep disabled" });
  if (!existsSync(SLEEPCTL)) return Promise.resolve({ ok: false, reason: "sleep.py missing" });
  return new Promise((resolve) => {
    execFile(PYTHON, [SLEEPCTL, ...args], { timeout: timeoutMs, maxBuffer: 4<<20, cwd: REPO, env: { ...process.env, SLEEP_HOME } },
      (err, stdout, stderr) => {
        const out = String(stdout || ""), errText = String(stderr || "");
        if (err && !out) return resolve({ ok: false, reason: (String(err.message) + " | " + errText).slice(0,200) });
        resolve({ ok: !err, stdout: out, stderr: errText });
      });
  });
}

function runHook(mode, payload, timeoutMs = 8000) {
  if (DISABLED()) return Promise.resolve();
  return new Promise((resolve) => {
    let done = false, child, timer;
    const finish = () => {
      if (done) return;
      done = true;
      if (timer) clearTimeout(timer);
      resolve();
    };
    try {
      child = spawn(PYTHON, ["-m", "sleepkit.hookio", mode], {
        cwd: REPO, env: { ...process.env, PYTHONPATH: REPO, SLEEP_HOME }, stdio: ["pipe", "ignore", "ignore"] });
      child.on("error", finish); child.on("close", finish);
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify(payload));
      timer = setTimeout(() => { try { child.kill("SIGTERM"); } catch { /* gone */ } finish(); }, timeoutMs);
      timer.unref?.();
    } catch { finish(); }
  });
}

function runDetached(args, logName = "hook.log") {
  if (DISABLED()) return;
  if (!existsSync(SLEEPCTL)) return note("wake-failed", "sleep.py missing");
  let fd; try { mkdirSync(LOG_DIR, { recursive: true }); fd = openSync(path.join(LOG_DIR, logName), "a"); } catch { /* no log */ }
  try {
    const child = spawn(PYTHON, [SLEEPCTL, ...args], { cwd: REPO, env: { ...process.env, SLEEP_HOME }, stdio: ["ignore", fd ?? "ignore", fd ?? "ignore"], detached: true });
    child.on("error", (e) => note("wake-failed", String(e?.message || e).slice(0,160)));
    child.unref(); note("wake", args.join(" ") + " pid=" + child.pid);
  } catch (e) { note("wake-failed", String(e?.message || e).slice(0,160)); }
  finally { if (fd !== undefined) { try { closeSync(fd); } catch { /* closed */ } } }
}

function wakeArgs() {
  const extra = [];
  if (process.env.SLEEP_MIN_RECORDS) extra.push("--min-records", String(process.env.SLEEP_MIN_RECORDS));
  if (process.env.SLEEP_MIN_INTERVAL) extra.push("--min-interval", String(process.env.SLEEP_MIN_INTERVAL));
  return ["sleep", ...extra];
}

const PATH_KEYS = ["filePath", "path", "file_path", "notebook_path"];

export default async ({ client, directory, worktree }) => {
  const log = (level, message) => {
    try { client?.app?.log?.({ body: { service: "sleep", level, message } }); } catch { /* noop */ }
  };

  return {
    "session.created": async (event) => {
      note("session.created", `cwd=${directory || worktree || "?"}`);
      log("info", "[sleep] session started, capture enabled");
    },
    "tool.execute.after": async (input) => {
      if (DISABLED()) return;
      let p = "";
      const args = input?.args || {};
      if (typeof args === "object") {
        for (const k of PATH_KEYS) { const v = args[k]; if (typeof v === "string" && v.trim()) { p = v.trim(); break; } }
      }
      if (!p) return; // calls without a path (bash/eval): don't spawn a process for nothing
      void runHook("capture-tool", {
        hook_event_name: "PostToolUse",
        tool_name: String(input?.tool || ""),
        tool_input: { file_path: p },
        session_id: "",
        cwd: directory || worktree || "",
      });
      note("capture", `posttool tool=${input?.tool} path=${p.slice(0, 200)}`);
    },
    "session.idle": async () => {
      if (DISABLED()) return;
      note("session.idle", "consolidate");
      runDetached(wakeArgs());
    },
  };
}

export const pluginName = "sleep-opencode";
