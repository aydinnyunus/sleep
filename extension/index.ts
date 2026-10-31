// @ts-nocheck
// Sleep extension for Pi/OmO (Layer 2b).
//
// Three jobs, all mechanical:
//   1. capture: move the user's own words and tool events that carry a path into the T0 buffer
//   2. publish: put the provisional memory back into the session as a message, and again after compaction
//   3. wake: trigger consolidation through the policy gate (a model call = never blocking)
//
// Why this file is not a hook: the engine (OmO/senpi 5.1.51) does not run Claude-style command
// hooks. Measured: a side-effect-free probe in ~/.omo/agent/hooks.json and <cwd>/.senpi/hooks.json,
// driven by a fresh `omo -p` and an interactive PTY session -> zero fires. So we carry the payload
// ourselves from the engine's own events; the payload fields are in the Claude shape the hook
// scripts expect. Parsing, gates and the fire counter live on the Python side
// (sleepkit/hookio.py); there are no decisions here, only transport.
//
// Nothing is ever written to stdout and no handler throws. T2 is never written from here:
// durable memory changes only through the human's `promote` command.
import { execFile, spawn } from "node:child_process";
import { appendFileSync, closeSync, existsSync, mkdirSync, openSync, realpathSync, renameSync, statSync } from "node:fs";
import os from "node:os";
import path from "path";
import { fileURLToPath } from "node:url";

const TRACE_LIMIT = 256 * 1024;
const PROVISIONAL_TYPE = "sleep-provisional-memory";
const DISABLED = () => process.env.SLEEP_DISABLE === "1";

/**
 * Find the repo root. The engine loads us through the ~/.omo/agent/extensions/sleep symlink;
 * if the symlink is not resolved, ".." points at the wrong directory. import.meta.dirname is not
 * reliable either: it is undefined in the engine's loader, so the extension failed to load with
 * "paths[0] ... got undefined" (measured). The candidate with a real sleep.py wins; SLEEP_REPO overrides.
 */
function findRepo() {
  const fromSelf = (() => {
    try {
      return path.join(path.dirname(realpathSync(fileURLToPath(import.meta.url))), "..");
    } catch {
      return "";
    }
  })();
  const candidates = [process.env.SLEEP_REPO, fromSelf, path.join(os.homedir(), "Research", "sleep")].filter(Boolean);
  for (const c of candidates) {
    const r = path.resolve(c);
    if (existsSync(path.join(r, "sleep.py"))) return r;
  }
  return path.resolve(candidates[0] || ".");
}

const REPO = findRepo();
const SLEEPCTL = process.env.SLEEPCTL || path.join(REPO, "sleep.py");
const SLEEP_HOME = process.env.SLEEP_HOME || path.join(os.homedir(), ".sleep");
const LOG_DIR = path.join(SLEEP_HOME, "logs");

/** Same shape as paths.now() in Python: local time + UTC offset (guards against timezone confusion). */
function ts() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  const abs = Math.abs(off);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T"
    + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds())
    + (off >= 0 ? "+" : "-") + pad(Math.floor(abs / 60)) + pad(abs % 60);
}

/** Load / capture / injection trace. Without it, "extension not loaded" and "loaded but did nothing" are indistinguishable. */
function note(kind, detail = "") {
  try {
    mkdirSync(LOG_DIR, { recursive: true });
    const file = path.join(LOG_DIR, "extension.log");
    let st = null;
    try { st = statSync(file); } catch { /* first write */ }
    if (st && st.size > TRACE_LIMIT) renameSync(file, file + ".1");
    appendFileSync(file, ts() + "\t" + kind + "\t" + String(detail).slice(0, 200) + "\n");
  } catch { /* tracing must never break the session */ }
}

function sessionId(ctx) {
  try { return ctx?.sessionManager?.getSessionId?.() || ""; } catch { return ""; }
}

/** Runs sleepctl and waits. Never throws. */
function sleepctl(args, timeoutMs = 20000) {
  if (DISABLED()) return Promise.resolve({ ok: false, reason: "sleep disabled (SLEEP_DISABLE=1)" });
  if (!existsSync(SLEEPCTL)) return Promise.resolve({ ok: false, reason: "sleep.py missing at " + SLEEPCTL });
  return new Promise((resolve) => {
    execFile("python3", [SLEEPCTL, ...args],
      { timeout: timeoutMs, maxBuffer: 4 << 20, cwd: REPO, env: { ...process.env, SLEEP_HOME } },
      (err, stdout, stderr) => {
        if (err && !stdout) return resolve({ ok: false, reason: String(err.message).slice(0, 200) });
        resolve({ ok: !err, stdout: String(stdout || ""), stderr: String(stderr || "") });
      });
  });
}

/** Passes a Claude-shaped payload to hookio's stdin adapter (the engine runs no hooks, we do the passing). */
function runHook(mode, payload, timeoutMs = 8000) {
  if (DISABLED()) return Promise.resolve();
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    try {
      const child = spawn("python3", ["-m", "sleepkit.hookio", mode], {
        cwd: REPO,
        env: { ...process.env, PYTHONPATH: REPO, SLEEP_HOME },
        stdio: ["pipe", "ignore", "ignore"],
      });
      const timer = setTimeout(() => { try { child.kill("SIGKILL"); } catch {} finish(); }, timeoutMs);
      timer.unref?.();
      child.on("error", finish);
      child.on("close", finish);
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify(payload));
    } catch { finish(); }
  });
}

/**
 * Consolidation is a model call (~1-2 min): detached like nohup, never blocking.
 * The blocking version was killed at the 20s timeout and could never produce a proposal.
 */
function runDetached(args, logName = "hook.log") {
  if (DISABLED()) return;
  if (!existsSync(SLEEPCTL)) return note("wake-failed", "sleep.py missing at " + SLEEPCTL);
  let fd;
  try {
    mkdirSync(LOG_DIR, { recursive: true });
    fd = openSync(path.join(LOG_DIR, logName), "a");
  } catch { /* without a log the output is lost, but the job must run anyway */ }
  try {
    const child = spawn(SLEEPCTL, args, {
      cwd: REPO,
      env: { ...process.env, SLEEP_HOME },
      stdio: ["ignore", fd ?? "ignore", fd ?? "ignore"],
      detached: true,
    });
    child.unref();
    note("wake", args.join(" ") + " pid=" + child.pid);
  } catch (err) {
    note("wake-failed", String(err?.message || err).slice(0, 160));
  } finally {
    if (fd !== undefined) { try { closeSync(fd); } catch {} }
  }
}

const PATH_KEYS = ["file_path", "path", "notebook_path", "filePath"];

function pathOf(input) {
  if (!input || typeof input !== "object") return "";
  for (const key of PATH_KEYS) {
    const v = input[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return "";
}

function provisionalMessage(content) {
  return {
    role: "custom",
    customType: PROVISIONAL_TYPE,
    content,
    display: false,
    details: { unverified: true },
    timestamp: Date.now(),
  };
}

function alreadyPublished(messages) {
  return (messages || []).some((m) => m && (m.customType === PROVISIONAL_TYPE
    || (m.message && m.message.customType === PROVISIONAL_TYPE)));
}

export default function sleep(pi, options = {}) {
  // tool_execution_end carries no arguments, so we map the path from the start event.
  const started = new Map();
  let publishedThisSession = false;

  async function publish(ctx, force = false) {
    if (DISABLED()) return;
    const result = await sleepctl(["inject", "--project", ctx?.cwd || ""]);
    const block = (result.stdout || "").trim();
    if (!result.ok || !block.includes("<provisional-memory")) {
      note("publish", `skipped: no block for project=${ctx?.cwd || "?"} ok=${result.ok} reason=${result.reason || ""}`);
      return;
    }
    try {
      pi.sendMessage(provisionalMessage(block), { triggerTurn: false });
      publishedThisSession = true;
      note("publish", `injected ${block.length} chars into project=${ctx?.cwd || "?"}`);
    } catch (err) {
      note("publish", `sendMessage failed: ${String(err?.message || err).slice(0, 120)}`);
      /* a session that rejects custom messages just runs without provisional memory */
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    note("session_start", `cwd=${ctx?.cwd || "?"} repo=${REPO} published=${publishedThisSession}`);
    await publish(ctx, true);
  });

  // The user's own words -> T0 source=user (the counterpart of UserPromptSubmit on the Claude side).
  // This handler never intercepts or modifies a message; it only copies.
  pi.on("input", async (event, ctx) => {
    if (DISABLED()) return;
    if (event?.source === "extension") return; // a message placed by an extension is not the user's own words
    const text = typeof event?.text === "string" ? event.text.trim() : "";
    if (!text) return;
    await runHook("capture", {
      hook_event_name: "UserPromptSubmit",
      prompt: text.slice(0, 8000),
      session_id: sessionId(ctx),
      cwd: ctx?.cwd || "",
    });
    note("capture", `userprompt chars=${text.length} src=${event?.source || "?"}`);
  });

  pi.on("tool_execution_start", async (event) => {
    if (DISABLED()) return;
    const key = String(event?.toolCallId ?? "");
    if (!key) return;
    started.set(key, { toolName: String(event?.toolName || ""), input: event?.args });
    if (started.size > 512) {
      const oldest = started.keys().next().value;
      if (oldest !== undefined) started.delete(oldest);
    }
  });

  // Counterpart of PostToolUse: only calls that succeeded and carry a path.
  pi.on("tool_execution_end", async (event, ctx) => {
    if (DISABLED()) return;
    const key = String(event?.toolCallId ?? "");
    const begin = started.get(key);
    started.delete(key);
    if (event?.isError) return; // reading a nonexistent file does not mean "touched the file"
    const p = pathOf(begin?.input);
    if (!p) return; // calls without a path (bash/eval): don't spawn a process for nothing
    await runHook("capture-tool", {
      hook_event_name: "PostToolUse",
      tool_name: begin?.toolName || String(event?.toolName || ""),
      tool_input: begin?.input,
      session_id: sessionId(ctx),
      cwd: ctx?.cwd || "",
    });
    note("capture", `posttool tool=${begin?.toolName || event?.toolName} path=${p.slice(0, 200)}`);
  });

  // Compaction drops the provisional message; provisional memory is exactly what must be recovered after it.
  pi.on("session_compact", async (_event, ctx) => {
    note("session_compact", `reason=${_event?.reason || "?"}`);
    publishedThisSession = false;
    await publish(ctx);
    runDetached(["sleep", "--force"]); // compress-on-sleep: never blocking
  });

  pi.on("context", (event) => {
    if (DISABLED() || publishedThisSession) return;
    if (alreadyPublished(event?.messages)) publishedThisSession = true;
  });

  // Counterpart of the Claude Stop hook: the turn ended, the gates live in Python.
  pi.on("agent_end", async (_event) => {
    if (DISABLED()) return;
    note("agent_end", JSON.stringify({ messages: Array.isArray(_event?.messages) ? _event.messages.length : null }));
    runDetached(["sleep",
      "--min-records", process.env.SLEEP_MIN_RECORDS || "3",
      "--min-interval", process.env.SLEEP_MIN_INTERVAL || "6"]);
  });

  pi.registerCommand("sleep", {
    description: "Memory sleep cycle: /sleep [status|capture <text>|sleep|review|promote <id>|replay]",
    handler: async (args, ctx) => {
      const parts = String(args || "").trim().split(/\s+/);
      const verb = parts[0] || "status";
      const rest = parts.slice(1);
      let invocation = ["status"];
      let detached = false;
      switch (verb) {
        case "status": invocation = ["status", "-v"]; break;
        case "capture": invocation = ["capture", "--text", rest.join(" ")]; break;
        case "sleep": detached = true; invocation = ["sleep", ...(rest.includes("force") ? ["--force"] : [])]; break;
        case "review": invocation = ["status", "-v"]; break;
        case "show": invocation = ["show", rest[0] || ""]; break;
        case "promote": invocation = ["promote", rest[0] || ""]; break;
        case "refuse": invocation = ["refuse", rest[0] || ""]; break;
        case "prune": invocation = ["prune"]; break;
        case "doctor": invocation = ["doctor"]; break;
        case "replay": detached = rest.includes("run"); invocation = ["replay", ...(detached ? ["--run"] : [])]; break;
        default:
          ctx.ui?.notify?.(`unknown /sleep subcommand: ${verb}`, "error");
          return;
      }
      note("command", `verb=${verb} argv=${invocation.join(" ")} cwd=${ctx?.cwd || "?"}`);
      if (detached) {
        runDetached(invocation, verb === "replay" ? "replay.log" : "hook.log");
        ctx.ui?.notify?.(`sleep: ${verb} runs detached, the result is written into ~.sleep/logs`, "info");
        return;
      }
      const out = await sleepctl(invocation);
      const text = (out.stdout || out.stderr || out.reason || "no output").trim();
      note("command_result", `verb=${verb} ok=${out.ok} ${text.split("\n")[0]}`);
      ctx.ui?.notify?.(text.slice(0, 4000), out.ok ? "info" : "error");
    },
  });
}
