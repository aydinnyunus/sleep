// @ts-nocheck
// Sleep extension for Pi/OmO (Layer 2b).
//
// Three jobs, all mechanical:
//   1. capture: move the human's own words, and tool events that carry a path, into the T0 buffer
//   2. publish: put the provisional memory in front of the model on every request, and re-read it
//      after a compaction dropped it
//   3. wake: trigger consolidation through the policy gate (a model call is never blocking)
//
// Why this file is not a hook: the engine (OmO/senpi 5.1.51) does not run Claude-style command
// hooks. Measured: seven candidate doors (the global hooks.json the engine resolves, the two legacy
// agent-dir paths, <cwd>/hooks.json, <cwd>/.senpi/hooks.json, a pristine CODING_AGENT_DIR with both
// a hooks.json and inline settings hooks) driven by five fresh sessions -> zero fires; the inline
// settings variant aborts the CLI instead. The runner is in the binary but has no call site in the
// session loop. So we carry the payload ourselves, from the engine's own events, in the Claude shape
// the Python adapters already parse (sleepkit/hookio.py).
//
// Two rules decide the shape of everything below:
//   provenance: an input event the human did not type is not the human's own words. The transport
//     forwards the engine's source marker and Python decides the trust (hookio.provenance).
//   policy: no gate, budget or threshold value is repeated here. The transport passes no knobs at
//     all, so argparse defaults stay the single source of truth; only a human typing /sleep may add
//     one (for example "force", which is a request for a model call, never something we infer).
//
// Nothing is ever written to stdout and no handler throws: this runs inside the agent loop. T2 is
// never written from here; durable memory changes only through the human's promote command.
import { execFile, spawn } from "node:child_process";
import { appendFileSync, closeSync, existsSync, mkdirSync, openSync, realpathSync, renameSync, statSync } from "node:fs";
import os from "node:os";
import path from "path";
import { fileURLToPath } from "node:url";

const TRACE_LIMIT = 256 * 1024;
const PROVISIONAL_TYPE = "sleep-provisional-memory";
const DISABLED = () => process.env.SLEEP_DISABLE === "1";

/**
 * Find the repo root. The engine loads us through the ~/.omo/agent/extensions/sleep symlink; if the
 * symlink is not resolved, ".." points at the wrong directory. import.meta.dirname is not reliable
 * either: it is undefined in the engine's loader, so the extension failed to load with
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

/** Runs sleepctl and waits. Never throws. The stderr head goes into the reason: an argparse error or a traceback is what the human needs to see. */
function sleepctl(args, timeoutMs = 20000) {
  if (DISABLED()) return Promise.resolve({ ok: false, reason: "sleep disabled (SLEEP_DISABLE=1)" });
  if (!existsSync(SLEEPCTL)) return Promise.resolve({ ok: false, reason: "sleep.py missing at " + SLEEPCTL });
  return new Promise((resolve) => {
    execFile("python3", [SLEEPCTL, ...args],
      { timeout: timeoutMs, maxBuffer: 4 << 20, cwd: REPO, env: { ...process.env, SLEEP_HOME } },
      (err, stdout, stderr) => {
        const out = String(stdout || "");
        const errText = String(stderr || "");
        if (err && !out) {
          return resolve({ ok: false, reason: (String(err.message) + " | " + errText).slice(0, 200) });
        }
        resolve({ ok: !err, stdout: out, stderr: errText });
      });
  });
}

/**
 * Pass a Claude-shaped payload to hookio's stdin adapter (the engine runs no hooks, we do the passing).
 * `track` registers the watchdog in the caller's session so a shutdown cannot clear another session's
 * watchdog (see the factory); it is cancelled here on a normal return, so nothing is held for the
 * full timeout after the child is gone. The payload is never logged: it carries the human's words.
 */
function runHook(mode, payload, track, timeoutMs = 8000) {
  if (DISABLED()) return Promise.resolve();
  return new Promise((resolve) => {
    let done = false;
    let cancel = () => {};
    const finish = () => { if (!done) { done = true; cancel(); resolve(); } };
    try {
      const child = spawn("python3", ["-m", "sleepkit.hookio", mode], {
        cwd: REPO,
        env: { ...process.env, PYTHONPATH: REPO, SLEEP_HOME },
        stdio: ["pipe", "ignore", "ignore"],
      });
      const watchdog = track
        ? track(() => { try { child.kill("SIGKILL"); } catch { /* already gone */ } finish(); }, timeoutMs)
        : null;
      if (watchdog) cancel = () => { try { clearTimeout(watchdog); } catch { /* settled */ } };
      child.on("error", finish);
      child.on("close", finish);
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify(payload));
    } catch {
      finish();
    }
  });
}

/**
 * Consolidation is a model call (~1-2 min): detached like nohup, never blocking. The blocking version
 * was killed at the 20s timeout and could never produce a proposal. The child has an "error" listener:
 * an unhandled one would be thrown at the engine process, not at us, and an ENOENT from a PATH that
 * cannot resolve the shebang's python3 is a realistic trigger.
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
    const child = spawn("python3", [SLEEPCTL, ...args], {
      cwd: REPO,
      env: { ...process.env, SLEEP_HOME },
      stdio: ["ignore", fd ?? "ignore", fd ?? "ignore"],
      detached: true,
    });
    child.on("error", (err) => note("wake-failed", String(err?.message || err).slice(0, 160)));
    child.unref();
    note("wake", args.join(" ") + " pid=" + child.pid);
  } catch (err) {
    note("wake-failed", String(err?.message || err).slice(0, 160));
  } finally {
    if (fd !== undefined) { try { closeSync(fd); } catch { /* closed */ } }
  }
}

const PATH_KEYS = ["file_path", "path", "notebook_path", "filePath"];

/** The path field of a tool call, as { key, path }: the key is kept so the adapter sees the same shape it would from a hook. */
function pathField(input) {
  if (!input || typeof input !== "object") return { key: "", path: "" };
  for (const key of PATH_KEYS) {
    const v = input[key];
    if (typeof v === "string" && v.trim()) return { key, path: v.trim() };
  }
  return { key: "", path: "" };
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

/** The knobs stay in Python. Only what a human explicitly set in the environment is passed. */
function wakeArgs() {
  const extra = [];
  if (process.env.SLEEP_MIN_RECORDS) extra.push("--min-records", String(process.env.SLEEP_MIN_RECORDS));
  if (process.env.SLEEP_MIN_INTERVAL) extra.push("--min-interval", String(process.env.SLEEP_MIN_INTERVAL));
  return ["sleep", ...extra];
}

export default function sleep(pi, options = {}) {
  // tool_execution_end carries no arguments, so the path is mapped from the start event. Only the
  // path is kept: start args of a write/edit call hold a whole file body, and holding up to 512 of
  // those for the life of a session is a leak of exactly the content we are trying not to duplicate.
  const started = new Map();

  // Session-scoped timers, so one session's shutdown cannot disarm another session's watchdog (the
  // engine hosts more than one session per process). Without tracking them the engine warns "left
  // handles open after session_shutdown ... 1 timer", the warning pi-lens and pi-mcp-adapter emit.
  const pendingTimers = new Set();
  function watchTimer(fn, ms) {
    const t = setTimeout(() => {
      pendingTimers.delete(t);
      try { fn(); } catch { /* a timer is not a handler, nothing upstream would catch it */ }
    }, ms);
    pendingTimers.add(t);
    t.unref?.();
    return t;
  }
  function clearTimers() {
    const n = pendingTimers.size;
    for (const t of pendingTimers) { try { clearTimeout(t); } catch { /* fired */ } }
    pendingTimers.clear();
    return n;
  }

  // The provisional block for this session. It is re-added to every request rather than persisted
  // once: a message written into the transcript outlives compaction, resume, /reload and /fork, so
  // injecting on those events can leave two copies in front of the model. Reading the store per
  // request makes a duplicate impossible and makes stale memory impossible the same way.
  let block = "";

  async function loadBlock(ctx) {
    if (DISABLED()) return "";
    const cwd = ctx?.cwd || "";
    // 5s, not the 20s default: this runs before the human's first reply is worth waiting on.
    const res = await sleepctl(["inject", "--project", cwd], 5000);
    const text = (res.stdout || "").trim();
    block = text.includes("<provisional-memory") ? text : "";
    note("publish", block
      ? `cached ${block.length} chars for project=${cwd || "?"}`
      : `nothing to publish project=${cwd || "?"} ok=${res.ok} ${res.reason || ""}`.slice(0, 180));
    return block;
  }

  pi.on("session_start", async (event, ctx) => {
    note("session_start", `reason=${event?.reason || "?"} cwd=${ctx?.cwd || "?"} repo=${REPO}`);
    await loadBlock(ctx);
  });

  // The human's own words -> T0 source=user (the counterpart of UserPromptSubmit on the Claude side).
  // This handler copies, it never intercepts: returning nothing means "continue". The capture is not
  // awaited: the buffer feeds a later consolidation, and making a person wait up to 8s for a python
  // spawn before their turn starts is a tax with no return.
  pi.on("input", (event, ctx) => {
    if (DISABLED()) return;
    const src = typeof event?.source === "string" ? event.source : "";
    if (src === "extension") return; // a message an extension placed is not the human's own words
    const text = typeof event?.text === "string" ? event.text.trim() : "";
    if (!text) return;
    void runHook("capture", {
      hook_event_name: "UserPromptSubmit",
      prompt: text.slice(0, 8000),
      session_id: sessionId(ctx),
      cwd: ctx?.cwd || "",
      pi_source: src, // rpc text wears the user's voice; Python decides what it is worth
    }, watchTimer);
    note("capture", `userprompt chars=${text.length} src=${src || "?"}`);
  });

  pi.on("tool_execution_start", async (event) => {
    if (DISABLED()) return;
    const key = String(event?.toolCallId ?? "");
    if (!key) return;
    const field = pathField(event?.args);
    started.set(key, {
      toolName: String(event?.toolName || ""),
      pathKey: field.key,
      path: field.path,
    });
    if (started.size > 2048) {
      const oldest = started.keys().next().value;
      if (oldest !== undefined) started.delete(oldest);
    }
  });

  // Counterpart of PostToolUse: only calls that succeeded and carry a path. Not awaited, same reason.
  pi.on("tool_execution_end", async (event, ctx) => {
    if (DISABLED()) return;
    const key = String(event?.toolCallId ?? "");
    const begin = started.get(key);
    started.delete(key);
    if (event?.isError) return; // reading a nonexistent file does not mean "touched the file"
    if (!begin?.path) return;   // calls without a path (bash/eval): don't spawn a process for nothing
    void runHook("capture-tool", {
      hook_event_name: "PostToolUse",
      tool_name: begin.toolName || String(event?.toolName || ""),
      tool_input: { [begin.pathKey || "file_path"]: begin.path },
      session_id: sessionId(ctx),
      cwd: ctx?.cwd || "",
    }, watchTimer);
    note("capture", `posttool tool=${begin.toolName || event?.toolName} path=${begin.path.slice(0, 200)}`);
  });

  // A compaction summarizes away the transcript, provisional memory included. The accepted/rejected
  // contract matters: a rejected compaction dropped nothing, so re-publishing would only add a copy.
  pi.on("session_compact", (event, ctx) => {
    if (event && event.accepted === false) {
      note("session_compact", `rejected cause=${String(event.rejectionCause || "?").slice(0, 80)}`);
      return;
    }
    note("session_compact", `reason=${event?.reason || "?"}`);
    runDetached(wakeArgs()); // gated: the transport never forces a model call
    block = "";
    void loadBlock(ctx);
  });

  // Idempotent cleanup: the engine warns about extensions that leave timers behind.
  pi.on("session_shutdown", async () => {
    const pending = clearTimers();
    started.clear();
    block = "";
    note("session_shutdown", `timers_cleared=${pending}`);
  });

  // Provisional memory goes in front of the model here, once per request, from the live store.
  // Declared read-only so the engine can share the live transcript instead of cloning it per turn.
  pi.on("context", (event) => {
    if (DISABLED() || !block) return;
    if (event?.source && event.source !== "agent") return; // a projection (summarizer input) is not the live request
    const messages = event?.messages;
    if (!Array.isArray(messages)) return;
    try {
      return { messages: [...messages, provisionalMessage(block)] };
    } catch {
      return; // a shape we cannot build must not break the request
    }
  }, { mutatesMessages: false });

  // Notification only: senpi may still auto-retry, auto-compact or continue with a queued message.
  pi.on("agent_end", async (_event) => {
    const n = Array.isArray(_event?.messages) ? _event.messages.length : null;
    note("agent_end", `messages=${n}`);
  });

  // Counterpart of the Claude Stop hook: nothing will run automatically after this point. The gates
  // and the budget live in Python; a model call must never block a session, so it is detached.
  pi.on("agent_settled", async (_event, ctx) => {
    if (DISABLED()) return;
    let idle = "?";
    try { idle = String(ctx?.isIdle?.()); } catch { /* older engine */ }
    note("agent_settled", `idle=${idle}`);
    runDetached(wakeArgs());
  });

  pi.registerCommand("sleep", {
    description: "Memory sleep cycle: /sleep [status|capture <text>|sleep|review|promote <id>|replay]",
    handler: async (args, ctx) => {
      const parts = String(args || "").trim().split(/\s+/).filter(Boolean);
      const verb = parts[0] || "status";
      const rest = parts.slice(1);
      const flag = (name) => rest.some((a) => a.replace(/^-+/, "").toLowerCase() === name);
      let invocation = ["status"];
      let detached = false;
      switch (verb) {
        case "status": invocation = ["status", "-v"]; break;
        case "capture": invocation = ["capture", "--text=" + rest.join(" "), "--source", "user", "--kind", "preference"]; break;
        case "sleep": detached = true; invocation = ["sleep", ...(flag("force") ? ["--force"] : [])]; break;
        case "review": invocation = ["status", "-v"]; break;
        case "show": invocation = ["show", rest[0] || ""]; break;
        case "promote": invocation = ["promote", rest[0] || ""]; break;
        case "refuse": invocation = ["refuse", rest[0] || ""]; break;
        case "prune": invocation = ["prune"]; break;
        case "doctor": invocation = ["doctor"]; break;
        case "replay": detached = flag("run"); invocation = ["replay", ...(detached ? ["--run"] : [])]; break;
        default:
          ctx.ui?.notify?.(`unknown /sleep subcommand: ${verb}`, "error");
          return;
      }
      // The verb and the count, never the values: a capture argument is the human's own text and this
      // log is not redacted (safety.redact runs on the way into the store, not on the way out here).
      note("command", `verb=${verb} argc=${rest.length} cwd=${ctx?.cwd || "?"}`);
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
