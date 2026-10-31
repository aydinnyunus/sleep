// @ts-nocheck
// Sleep extension for Pi/OmO (Katman 2b).
//
// Three jobs, all mechanical:
//   1. capture: kullanici sozunu ve path tasiyan arac olaylarini T0 tamponuna tasir
//   2. publish: gecici hafizayi oturuma acik mesaj olarak geri koy, compaction'dan sonra tekrar koy
//   3. wake: konsolidasyonu politika kapisindan gecirerek uyut (model cagrisi = beklemeden)
//
// Neden bu dosya bir hook degil: motor (OmO/senpi 5.1.51) Claude-bicimli command hook
// calistirmiyor. Olculdu: yan etkisiz probe ~/.omo/agent/hooks.json ve <cwd>/.senpi/hooks.json
// icinde, taze `omo -p` ve PTY ile surulmus interaktif oturum -> sifir ates. Bu yuzden
// payload'i motorun kendi olaylarindan kendimiz tasiyoruz; payload alanlari hook
// betiklerinin bekledigi Claude bicimindedir. Ayristirma, kapilar ve ates sayaci
// Python tarafinda (sleepkit/hookio.py); burada karar yok, sadece tasima var.
//
// Stdout'a hicbir sey yazilmaz, hicbir handler firlatmaz. T2'ye buradan yazilmaz:
// kalici hafiza yalnizca insanin `promote` komutuyla degisir.
import { execFile, spawn } from "node:child_process";
import { appendFileSync, closeSync, existsSync, mkdirSync, openSync, realpathSync, renameSync, statSync } from "node:fs";
import os from "node:os";
import path from "path";
import { fileURLToPath } from "node:url";

const TRACE_LIMIT = 256 * 1024;
const PROVISIONAL_TYPE = "sleep-provisional-memory";
const DISABLED = () => process.env.SLEEP_DISABLE === "1";

/**
 * Repo kokunu bul. Motor bizi ~/.omo/agent/extensions/sleep symlink'i uzerinden yukluyor;
 * symlink cozulmezse ".." yanlis dizine gider. import.meta.dirname de guvenilir degil:
 * motor yukleyicisinde undefined => "paths[0] ... got undefined" ile extension hic
 * yuklenmiyordu (olculdu). sleep.py gercekten olan aday secilir; SLEEP_REPO ile ezilebilir.
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

/** Python'daki paths.now() ile ayni bicim: yerel saat + UTC offset (saat dilimi yanilmasina karsi). */
function ts() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  const abs = Math.abs(off);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T"
    + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds())
    + (off >= 0 ? "+" : "-") + pad(Math.floor(abs / 60)) + pad(abs % 60);
}

/** Yuklenme/yakalama/enjeksiyon izi. olmadan "extension yuklenmedi" ile "yuklendi, bir sey yapmadi" ayirt edilemez. */
function note(kind, detail = "") {
  try {
    mkdirSync(LOG_DIR, { recursive: true });
    const file = path.join(LOG_DIR, "extension.log");
    let st = null;
    try { st = statSync(file); } catch { /* ilk yazim */ }
    if (st && st.size > TRACE_LIMIT) renameSync(file, file + ".1");
    appendFileSync(file, ts() + "\t" + kind + "\t" + String(detail).slice(0, 200) + "\n");
  } catch { /* iz, oturumu asla kiramaz */ }
}

function sessionId(ctx) {
  try { return ctx?.sessionManager?.getSessionId?.() || ""; } catch { return ""; }
}

/** sleepctl'i bekleyerek calistirir. Asla firlatmaz. */
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

/** hookio'nun stdin adapter'ina Claude-bicimli payload tasir (motor hook'u calistirmiyor, biz tasiriz). */
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
 * Konsolidasyon bir model cagrisidir (~1-2 dk): nohup gibi ayrik, bekletmeden.
 * Bekleten surum 20 sn timeout'ta olduruluyor ve hicbir oneri uretemiyordu.
 */
function runDetached(args, logName = "hook.log") {
  if (DISABLED()) return;
  if (!existsSync(SLEEPCTL)) return note("wake-failed", "sleep.py missing at " + SLEEPCTL);
  let fd;
  try {
    mkdirSync(LOG_DIR, { recursive: true });
    fd = openSync(path.join(LOG_DIR, logName), "a");
  } catch { /* log yoksa cikti kaybolur, is yine de gorsun */ }
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
  // tool_execution_end'de arguman olmuyor; path'i start'tan eslestiriyoruz.
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
      /* custom mesaj kabul etmeyen bir oturum hafizasiz calisir */
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    note("session_start", `cwd=${ctx?.cwd || "?"} repo=${REPO} published=${publishedThisSession}`);
    await publish(ctx, true);
  });

  // Kullanicinin kendi sozu -> T0 source=user (Claude tarafindaki UserPromptSubmit'in karsiligi).
  // Bu handler hicbir mesaji yakalamaz/degistirmez; sadece kopyalar.
  pi.on("input", async (event, ctx) => {
    if (DISABLED()) return;
    if (event?.source === "extension") return; // extension'in koydugu mesaj kullanici sozu degil
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

  // PostToolUse karsiligi: yalnizca basariyla bitmis ve path tasiyan cagrilar.
  pi.on("tool_execution_end", async (event, ctx) => {
    if (DISABLED()) return;
    const key = String(event?.toolCallId ?? "");
    const begin = started.get(key);
    started.delete(key);
    if (event?.isError) return; // olmayan dosyayi okumak "dosyaya dokundu" demek degil
    const p = pathOf(begin?.input);
    if (!p) return; // bash/eval gibi pathsiz cagrilar: bosuna surec acma
    await runHook("capture-tool", {
      hook_event_name: "PostToolUse",
      tool_name: begin?.toolName || String(event?.toolName || ""),
      tool_input: begin?.input,
      session_id: sessionId(ctx),
      cwd: ctx?.cwd || "",
    });
    note("capture", `posttool tool=${begin?.toolName || event?.toolName} path=${p.slice(0, 200)}`);
  });

  // Compaction onceki gecici mesaji siler; gecici hafiza tam da bundan sonra kurtarilmali.
  pi.on("session_compact", async (_event, ctx) => {
    note("session_compact", `reason=${_event?.reason || "?"}`);
    publishedThisSession = false;
    await publish(ctx);
    runDetached(["sleep", "--force"]); // compress-on-sleep: bekletmeden
  });

  pi.on("context", (event) => {
    if (DISABLED() || publishedThisSession) return;
    if (alreadyPublished(event?.messages)) publishedThisSession = true;
  });

  // Claude tarafindaki Stop hook'unun karsiligi: tur bitti, kapilar Python'da.
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
        ctx.ui?.notify?.(`sleep: ${verb} ayrik calisiyor, sonuc ~.sleep/logs icine yaziliyor`, "info");
        return;
      }
      const out = await sleepctl(invocation);
      const text = (out.stdout || out.stderr || out.reason || "no output").trim();
      note("command_result", `verb=${verb} ok=${out.ok} ${text.split("\n")[0]}`);
      ctx.ui?.notify?.(text.slice(0, 4000), out.ok ? "info" : "error");
    },
  });
}
