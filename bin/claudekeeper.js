#!/usr/bin/env node
"use strict";

/*
 * claudekeeper
 *
 * A supervisor for Claude Code that keeps a long task alive across usage limits.
 * It runs Claude Code in headless mode, watches the stream for a usage limit,
 * and when one is hit it either switches to the next model in the chain (for
 * example Fable to the latest Opus) and resumes the same session immediately,
 * or, when every model in the chain is limited, parses the reset time and waits
 * before resuming. The goal is simple: you should not have to babysit a run
 * that stops because a limit was reached.
 */

const { spawn } = require("node:child_process");
const readline = require("node:readline");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// ---------------------------------------------------------------------------
// Defaults and configuration
// ---------------------------------------------------------------------------

const ENV = process.env;

const DEFAULTS = {
  claudeBin: ENV.CLAUDEKEEPER_CLAUDE_BIN || "claude",
  primaryModel: ENV.CLAUDEKEEPER_PRIMARY_MODEL || "fable",
  fallbackModels: splitList(ENV.CLAUDEKEEPER_FALLBACK_MODELS) || ["opus"],
  // When the reset time cannot be read from the output, wait this many minutes
  // before trying again.
  retryMinutes: numberOr(ENV.CLAUDEKEEPER_RETRY_MINUTES, 20),
  // Never sleep longer than this in one wait, as a safety cap.
  waitCapMinutes: numberOr(ENV.CLAUDEKEEPER_WAIT_CAP_MINUTES, 360),
  // Small buffer added after a parsed reset time so we do not wake a touch early.
  resetBufferSeconds: numberOrZero(ENV.CLAUDEKEEPER_RESET_BUFFER_SECONDS, 20),
  maxResumes: numberOr(ENV.CLAUDEKEEPER_MAX_RESUMES, 200),
};

const PROJECTS_DIR = path.join(os.homedir(), ".claude", "projects");

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function splitList(v) {
  if (!v) return null;
  const parts = String(v).split(",").map((s) => s.trim()).filter(Boolean);
  return parts.length ? parts : null;
}

function numberOr(v, dflt) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : dflt;
}

// Like numberOr but zero is a valid value (used for buffers that may be 0).
function numberOrZero(v, dflt) {
  if (v === undefined || v === null || v === "") return dflt;
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : dflt;
}

const COLOR = process.stdout.isTTY && !ENV.NO_COLOR;
function paint(code, s) {
  return COLOR ? `[${code}m${s}[0m` : s;
}
const c = {
  dim: (s) => paint("2", s),
  bold: (s) => paint("1", s),
  green: (s) => paint("32", s),
  yellow: (s) => paint("33", s),
  red: (s) => paint("31", s),
  cyan: (s) => paint("36", s),
};

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function log(msg) {
  process.stderr.write(`${c.dim(stamp())} ${c.cyan("keeper")} ${msg}\n`);
}
function warn(msg) {
  process.stderr.write(`${c.dim(stamp())} ${c.yellow("keeper")} ${msg}\n`);
}
function fail(msg) {
  process.stderr.write(`${c.dim(stamp())} ${c.red("keeper")} ${msg}\n`);
}

function sleep(ms) {
  // The timer must stay referenced. During a reset wait there is no child
  // process holding the event loop open, so an unref here would let Node exit
  // early and skip the resume.
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Outcome classification
//
// Given everything a run printed plus its exit code, decide what happened.
// Kinds:
//   ok       the run finished a turn without hitting a limit
//   limit    a usage limit was reached (resumable by waiting or switching)
//   fatal    an unrecoverable problem (bad auth, no credit, invalid request)
//   unknown  the run died in a way we do not recognise
// ---------------------------------------------------------------------------

const LIMIT_RE = /usage limit reached|resource[_ ]exhausted|billing_error/i;
const FATAL_RE = /credit balance (?:is )?too low|invalid api key|please run \/login|authentication failed|oauth token (?:expired|revoked)|401 unauthorized|403 forbidden|bad credentials/i;

function classifyOutcome({ combined, code, resultText, isError }) {
  const hay = `${combined || ""}\n${resultText || ""}`;

  // A hard billing or auth wall cannot be waited out.
  if (FATAL_RE.test(hay)) {
    return { kind: "fatal", reason: firstMatch(hay, FATAL_RE) };
  }

  // A usage limit is resumable.
  if (LIMIT_RE.test(hay)) {
    return { kind: "limit", resetEpochMs: parseResetEpochMs(hay) };
  }

  // A clean finish: the process exited zero and did not flag an error.
  if (code === 0 && !isError) {
    return { kind: "ok", resultText: resultText || "" };
  }

  // Exited zero but flagged is_error without a limit signature: still treat the
  // turn as finished so we do not loop forever, but note it.
  if (code === 0) {
    return { kind: "ok", resultText: resultText || "", flaggedError: true };
  }

  return { kind: "unknown", code };
}

function firstMatch(s, re) {
  const m = s.match(re);
  return m ? m[0] : "";
}

// ---------------------------------------------------------------------------
// Reset time parsing
//
// Claude Code exposes the reset moment in a few shapes across versions. We try,
// in order: the pipe format ("usage limit reached|<epoch>"), a numeric reset
// field, an ISO reset field, and finally a human phrase ("resets at 3pm").
// Returns epoch milliseconds, or null when nothing could be read.
// ---------------------------------------------------------------------------

function parseResetEpochMs(text) {
  if (!text) return null;

  // 1) Pipe format: usage limit reached|1712345678  (seconds or millis)
  let m = text.match(/usage limit reached\s*\|\s*(\d{9,13})/i);
  if (m) return normaliseEpoch(m[1]);

  // 2) A numeric reset field: "resetsAt": 1712345678  /  reset_at, resetTime
  m = text.match(/"?reset(?:sAt|_at|Time)"?\s*[:=]\s*"?(\d{9,13})"?/i);
  if (m) return normaliseEpoch(m[1]);

  // 3) An ISO reset field: "resetsAt": "2026-07-10T18:00:00Z"
  m = text.match(/"?reset(?:sAt|_at|Time)"?\s*[:=]\s*"([0-9T:\-+.Z ]{10,40})"/i);
  if (m) {
    const t = Date.parse(m[1]);
    if (Number.isFinite(t)) return t;
  }

  // 4) A human phrase: "resets at 3pm" / "reset at 3:30 PM" / "will reset at 15:00"
  m = text.match(/reset(?:s)?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/i);
  if (m) {
    const now = new Date();
    let hour = parseInt(m[1], 10);
    const min = m[2] ? parseInt(m[2], 10) : 0;
    const ampm = m[3] ? m[3].toLowerCase() : null;
    if (ampm === "pm" && hour < 12) hour += 12;
    if (ampm === "am" && hour === 12) hour = 0;
    if (hour > 23 || min > 59) return null;
    const target = new Date(now);
    target.setHours(hour, min, 0, 0);
    if (target.getTime() <= now.getTime()) target.setDate(target.getDate() + 1);
    return target.getTime();
  }

  return null;
}

function normaliseEpoch(digits) {
  const n = Number(digits);
  if (!Number.isFinite(n)) return null;
  // 13 digits is millis, 10 digits is seconds.
  return String(digits).length >= 13 ? n : n * 1000;
}

// ---------------------------------------------------------------------------
// Model switch state machine
//
// Given the current position in the model chain and the fact that a limit was
// hit, decide the next action:
//   switch  advance to the next model and resume immediately
//   wait    every model is limited, so sleep until reset then restart the chain
// ---------------------------------------------------------------------------

// Collapse an ordered list of models to distinct entries, keeping the first of
// each. This avoids a wasted resume when, say, the primary is opus and the
// default fallback is also opus.
function dedupeChain(models) {
  const seen = new Set();
  const out = [];
  for (const m of models) {
    if (!m) continue;
    const key = String(m).toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(m);
  }
  return out;
}

function decideOnLimit({ modelIndex, chainLength, switchEnabled, waitEnabled }) {
  if (switchEnabled && modelIndex < chainLength - 1) {
    return { action: "switch", nextIndex: modelIndex + 1 };
  }
  if (waitEnabled) {
    return { action: "wait", nextIndex: 0 };
  }
  return { action: "stop" };
}

// ---------------------------------------------------------------------------
// Running a single Claude Code turn in headless streaming mode
// ---------------------------------------------------------------------------

function runOnce({ opts, model, sessionId, prompt }) {
  return new Promise((resolve) => {
    const args = ["--print", "--output-format", "stream-json", "--verbose", "--model", model];
    if (sessionId) args.push("--resume", sessionId);
    if (opts.permissionMode) args.push("--permission-mode", opts.permissionMode);
    if (opts.dangerous) args.push("--dangerously-skip-permissions");
    for (const dir of opts.addDir) args.push("--add-dir", dir);
    // Let Claude Code degrade to its own fallback on transient overload (529),
    // independent of our usage limit handling.
    if (opts.overloadFallback) args.push("--fallback-model", opts.overloadFallback);
    for (const extra of opts.passthrough) args.push(extra);

    const child = spawn(opts.claudeBin, args, {
      cwd: opts.cwd,
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });

    let combined = "";
    let resultText = "";
    let isError = false;
    let newSessionId = sessionId || null;
    let spawnError = null;

    child.on("error", (err) => {
      spawnError = err;
    });

    // Feed the prompt on stdin so we never fight argument escaping.
    if (prompt != null) {
      child.stdin.write(prompt);
    }
    child.stdin.end();

    const rl = readline.createInterface({ input: child.stdout });
    rl.on("line", (line) => {
      combined += line + "\n";
      let ev = null;
      try {
        ev = JSON.parse(line);
      } catch {
        // Not JSON. Keep it in combined for pattern scanning and stream it.
        if (opts.stream) process.stdout.write(line + "\n");
        return;
      }
      handleEvent(ev);
    });

    function handleEvent(ev) {
      if (!ev || typeof ev !== "object") return;
      if (ev.session_id) newSessionId = ev.session_id;

      if (ev.type === "assistant" && ev.message && Array.isArray(ev.message.content)) {
        for (const block of ev.message.content) {
          if (block.type === "text" && block.text) {
            if (opts.stream) process.stdout.write(block.text);
          }
        }
      }

      if (ev.type === "result") {
        if (typeof ev.result === "string") resultText += ev.result;
        if (ev.is_error) isError = true;
        if (ev.subtype && /error/i.test(ev.subtype)) isError = true;
      }

      // Some versions emit a discrete error event.
      if (ev.type === "error" || ev.is_error) {
        isError = true;
        if (ev.error && typeof ev.error === "string") combined += ev.error + "\n";
        if (ev.message && typeof ev.message === "string") combined += ev.message + "\n";
      }
    }

    let stderr = "";
    child.stderr.on("data", (d) => {
      const s = d.toString();
      stderr += s;
      combined += s;
      if (opts.stream) process.stderr.write(c.dim(s));
    });

    child.on("close", (code) => {
      if (spawnError) {
        resolve({
          outcome: { kind: "spawn_error", reason: spawnError.message },
          sessionId: newSessionId,
        });
        return;
      }
      if (opts.stream) process.stdout.write("\n");
      const outcome = classifyOutcome({ combined, code, resultText, isError });
      resolve({ outcome, sessionId: newSessionId, combined, stderr, code });
    });

    // Allow the supervisor to kill the child on Ctrl+C.
    currentChild = child;
  });
}

let currentChild = null;

// ---------------------------------------------------------------------------
// Waiting for a reset
// ---------------------------------------------------------------------------

function formatDuration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

async function waitForReset(resetEpochMs, opts) {
  const capMs = opts.waitCapMinutes * 60 * 1000;
  const now = Date.now();
  let targetMs;
  if (resetEpochMs && resetEpochMs > now) {
    targetMs = resetEpochMs + opts.resetBufferSeconds * 1000;
    const untilKnown = targetMs - now;
    if (untilKnown > capMs) {
      warn(`reset is ${formatDuration(untilKnown)} away, longer than the ${opts.waitCapMinutes}m cap. Waiting the cap, then retrying.`);
      targetMs = now + capMs;
    } else {
      log(`all models limited. Waiting ${formatDuration(untilKnown)} for reset at ${new Date(resetEpochMs).toLocaleString()}.`);
    }
  } else {
    targetMs = now + Math.min(opts.retryMinutes * 60 * 1000, capMs);
    warn(`could not read a reset time. Retrying in ${formatDuration(targetMs - now)}.`);
  }

  // Sleep in short slices so Ctrl+C stays responsive and we can show progress.
  while (Date.now() < targetMs) {
    const remain = targetMs - Date.now();
    const slice = Math.min(remain, 30 * 1000);
    if (remain > 60 * 1000 && process.stderr.isTTY) {
      process.stderr.write(`\r${c.dim(stamp())} ${c.cyan("keeper")} waiting, ${formatDuration(remain)} left   `);
    }
    await sleep(slice);
  }
  if (process.stderr.isTTY) process.stderr.write("\n");
}

// ---------------------------------------------------------------------------
// The supervise loop
// ---------------------------------------------------------------------------

async function supervise(opts) {
  const chain = dedupeChain([opts.primaryModel, ...opts.fallbackModels]);
  let modelIndex = 0;
  let sessionId = opts.sessionId || null;
  let prompt = opts.prompt;
  let resumes = 0;
  const started = Date.now();

  log(`starting with model chain ${c.bold(chain.join(" then "))}.`);
  if (sessionId) log(`resuming session ${c.bold(sessionId)}.`);

  while (true) {
    const model = chain[modelIndex];
    if (resumes === 0) {
      log(`running on ${c.bold(model)}.`);
    } else {
      log(`resume #${resumes} on ${c.bold(model)}.`);
    }

    const { outcome, sessionId: sid } = await runOnce({ opts, model, sessionId, prompt });
    if (sid) sessionId = sid;

    if (outcome.kind === "ok") {
      // The turn finished. If the user asked us to keep going, send another
      // continue; otherwise we are done.
      if (outcome.flaggedError) {
        warn("the run flagged an error that was not a usage limit. Treating the turn as finished.");
      }
      if (opts.keepGoing > 0) {
        opts.keepGoing -= 1;
        prompt = opts.continuePrompt;
        modelIndex = 0; // fresh turns start back on the primary model
        resumes += 1;
        log(`turn finished. Auto continuing, ${opts.keepGoing} left.`);
        continue;
      }
      log(c.green(`done. Session ${sessionId} finished after ${formatDuration(Date.now() - started)}.`));
      return { ok: true, sessionId };
    }

    if (outcome.kind === "fatal") {
      fail(`unrecoverable: ${outcome.reason || "authentication or billing problem"}. Not resuming.`);
      return { ok: false, sessionId, reason: outcome.reason };
    }

    if (outcome.kind === "spawn_error") {
      fail(`could not launch ${opts.claudeBin}: ${outcome.reason}`);
      return { ok: false, reason: outcome.reason };
    }

    if (outcome.kind === "unknown") {
      fail(`the run exited with code ${outcome.code} and no recognised signal. Stopping to avoid a blind loop.`);
      return { ok: false, sessionId, reason: `exit ${outcome.code}` };
    }

    // outcome.kind === "limit"
    if (resumes >= opts.maxResumes) {
      fail(`hit the resume cap of ${opts.maxResumes}. Stopping.`);
      return { ok: false, sessionId, reason: "resume cap" };
    }

    const decision = decideOnLimit({
      modelIndex,
      chainLength: chain.length,
      switchEnabled: opts.switchEnabled,
      waitEnabled: opts.waitEnabled,
    });

    if (decision.action === "switch") {
      const from = chain[modelIndex];
      const to = chain[decision.nextIndex];
      modelIndex = decision.nextIndex;
      warn(`${c.bold(from)} usage limit reached. Switching to ${c.bold(to)} and resuming the same session.`);
      // Continue the interrupted work rather than repeat the whole prompt.
      prompt = sessionId ? opts.continuePrompt : prompt;
      resumes += 1;
      continue;
    }

    if (decision.action === "wait") {
      warn(`${c.bold(chain[modelIndex])} usage limit reached and no model left to switch to.`);
      await waitForReset(outcome.resetEpochMs, opts);
      modelIndex = 0;
      prompt = sessionId ? opts.continuePrompt : prompt;
      resumes += 1;
      log("reset window passed. Resuming on the primary model.");
      continue;
    }

    fail("a limit was reached but switching and waiting are both disabled. Stopping.");
    return { ok: false, sessionId, reason: "limit, no strategy" };
  }
}

// ---------------------------------------------------------------------------
// list subcommand
// ---------------------------------------------------------------------------

function listSessions(limit) {
  let dirs = [];
  try {
    dirs = fs.readdirSync(PROJECTS_DIR);
  } catch {
    return [];
  }
  const rows = [];
  for (const dir of dirs) {
    const full = path.join(PROJECTS_DIR, dir);
    let files = [];
    try {
      files = fs.readdirSync(full).filter((f) => f.endsWith(".jsonl"));
    } catch {
      continue;
    }
    for (const f of files) {
      const p = path.join(full, f);
      let st;
      try {
        st = fs.statSync(p);
      } catch {
        continue;
      }
      rows.push({
        id: f.replace(/\.jsonl$/, ""),
        project: dir,
        mtime: st.mtimeMs,
        sizeKb: Math.round(st.size / 1024),
      });
    }
  }
  rows.sort((a, b) => b.mtime - a.mtime);
  return rows.slice(0, limit);
}

function decodeProject(slug) {
  // The slug replaces both "/" and "." with "-", so the original path cannot be
  // recovered exactly. We show a best effort readable form.
  return slug.replace(/^-/, "/").replace(/-/g, "/");
}

function cmdList(args) {
  const limit = numberOr(args.limit, 20);
  const rows = listSessions(limit);
  if (args.json) {
    process.stdout.write(JSON.stringify(rows, null, 2) + "\n");
    return 0;
  }
  if (!rows.length) {
    warn("no sessions found under ~/.claude/projects.");
    return 0;
  }
  process.stdout.write(c.bold("Recent Claude Code sessions\n"));
  for (const r of rows) {
    const when = new Date(r.mtime).toLocaleString();
    process.stdout.write(
      `${c.cyan(r.id)}  ${c.dim(when)}  ${String(r.sizeKb).padStart(6)}kb  ${c.dim(decodeProject(r.project))}\n`
    );
  }
  process.stdout.write(
    c.dim(`\nResume one with:  claudekeeper resume ${rows[0].id}\n`)
  );
  return 0;
}

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = { _: [], passthrough: [] };
  let i = 0;
  // Everything after a lone "--" is passed straight to claude.
  while (i < argv.length) {
    const a = argv[i];
    if (a === "--") {
      args.passthrough = argv.slice(i + 1);
      break;
    }
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const eq = key.indexOf("=");
      if (eq >= 0) {
        args[key.slice(0, eq)] = key.slice(eq + 1);
      } else if (i + 1 < argv.length && !argv[i + 1].startsWith("-")) {
        args[key] = argv[++i];
      } else {
        args[key] = true;
      }
    } else if (a.startsWith("-") && a.length === 2) {
      const short = { m: "model", p: "prompt", c: "cwd", s: "session" }[a[1]];
      if (short && i + 1 < argv.length && !argv[i + 1].startsWith("-")) {
        args[short] = argv[++i];
      } else {
        args[a[1]] = true;
      }
    } else {
      args._.push(a);
    }
    i++;
  }
  return args;
}

function buildOpts(args, prompt, sessionId) {
  const fallbackModels =
    splitList(args["fallback"]) ||
    (args["no-fallback"] ? [] : DEFAULTS.fallbackModels);

  return {
    claudeBin: args["claude-bin"] || DEFAULTS.claudeBin,
    primaryModel: args.model || DEFAULTS.primaryModel,
    fallbackModels,
    cwd: args.cwd ? path.resolve(args.cwd) : process.cwd(),
    sessionId: sessionId || null,
    prompt,
    continuePrompt: args["continue-prompt"] || "Continue exactly where you left off.",
    keepGoing: Math.max(0, Math.floor(numberOr(args["keep-going"], 0)) || 0),
    switchEnabled: !args["no-switch"],
    waitEnabled: !args["no-wait"],
    permissionMode: args["permission-mode"] || null,
    dangerous: !!args["dangerously-skip-permissions"] || !!args["yes"],
    addDir: [].concat(args["add-dir"] || []).filter((x) => typeof x === "string"),
    overloadFallback: args["overload-fallback"] || null,
    retryMinutes: numberOr(args["retry-minutes"], DEFAULTS.retryMinutes),
    waitCapMinutes: numberOr(args["wait-cap-minutes"], DEFAULTS.waitCapMinutes),
    resetBufferSeconds: DEFAULTS.resetBufferSeconds,
    maxResumes: numberOr(args["max-resumes"], DEFAULTS.maxResumes),
    stream: !args["quiet"],
    passthrough: args.passthrough || [],
  };
}

// ---------------------------------------------------------------------------
// Help
// ---------------------------------------------------------------------------

const HELP = `${c.bold("claudekeeper")} keeps a Claude Code task alive across usage limits.

When a run stops because a usage limit was reached, claudekeeper switches to the
next model in the chain (for example Fable to the latest Opus) and resumes the
same session. When every model is limited it reads the reset time and waits,
then resumes on the primary model.

${c.bold("Usage")}
  claudekeeper watch [-- claude args]  supervise a live interactive session
  claudekeeper install-shell           wrap the claude command so every session is watched
  claudekeeper run [prompt]            start a supervised unattended task
  claudekeeper resume <id|--last>      resume an existing session and keep it alive
  claudekeeper list                    show recent sessions to pick from
  claudekeeper help                    show this help

${c.bold("Transparent mode")}
  Run  claudekeeper install-shell --write  once, then use claude exactly as
  normal. Your live sessions look identical, but on a usage limit the supervisor
  switches Fable to the latest Opus and continues, or waits for reset and
  continues. Turn it off with  claudekeeper uninstall-shell.

${c.bold("Common options")}
  -m, --model <name>          primary model (default ${DEFAULTS.primaryModel})
      --fallback a,b          fallback chain (default ${DEFAULTS.fallbackModels.join(",")})
      --no-fallback           never switch models, only wait for reset
      --no-switch             same as no fallback, always wait
      --no-wait               never wait, stop once every model is limited
      --keep-going N          after a turn finishes, auto continue up to N times
      --continue-prompt <s>   text used to resume after a limit or to continue
  -c, --cwd <dir>             working directory for the task
      --permission-mode <m>   claude permission mode (for example acceptEdits)
      --dangerously-skip-permissions   pass through to claude (alias: --yes)
      --add-dir <dir>         extra directory claude may access (repeatable)
      --retry-minutes N       wait when a reset time cannot be read (default ${DEFAULTS.retryMinutes})
      --wait-cap-minutes N    never wait longer than this at once (default ${DEFAULTS.waitCapMinutes})
      --max-resumes N         stop after this many resumes (default ${DEFAULTS.maxResumes})
      --quiet                 do not stream the model output, only keeper logs
      -- <args>               everything after -- is passed straight to claude

${c.bold("Environment")}
  CLAUDEKEEPER_PRIMARY_MODEL, CLAUDEKEEPER_FALLBACK_MODELS,
  CLAUDEKEEPER_RETRY_MINUTES, CLAUDEKEEPER_WAIT_CAP_MINUTES,
  CLAUDEKEEPER_MAX_RESUMES, CLAUDEKEEPER_CLAUDE_BIN

${c.bold("Examples")}
  claudekeeper run "Refactor the billing module and run the tests"
  claudekeeper run --keep-going 20 "Work through the TODO list until done"
  claudekeeper resume --last
  claudekeeper resume 04887427-cb7d-4816-b6ac-b9ec73ddfe06 -m fable
`;

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

async function main() {
  const argv = process.argv.slice(2);
  const args = parseArgs(argv);
  const cmd = args._[0] || (args.version || args.v ? "version" : "help");

  if (args.version || args.v || cmd === "version") {
    const pkg = readPkg();
    process.stdout.write(`claudekeeper ${pkg.version || "0.0.0"}\n`);
    return 0;
  }

  if (cmd === "help" || args.help || args.h) {
    process.stdout.write(HELP);
    return 0;
  }

  if (cmd === "list") {
    return cmdList(args);
  }

  if (cmd === "watch") {
    return cmdWatch(args);
  }

  if (cmd === "install-shell") {
    return cmdInstallShell(args);
  }

  if (cmd === "uninstall-shell") {
    return cmdUninstallShell(args);
  }

  installSignalHandlers();

  if (cmd === "run") {
    const prompt = args.prompt || args._.slice(1).join(" ").trim();
    if (!prompt) {
      fail("run needs a prompt. Example: claudekeeper run \"Do the thing\"");
      return 2;
    }
    const opts = buildOpts(args, prompt, null);
    const res = await supervise(opts);
    return res.ok ? 0 : 1;
  }

  if (cmd === "resume") {
    let id = args._[1] || args.session || null;
    if (args.last || id === "--last") {
      const rows = listSessions(1);
      if (!rows.length) {
        fail("no sessions found to resume.");
        return 2;
      }
      id = rows[0].id;
      log(`resuming the most recent session ${c.bold(id)}.`);
    }
    if (!id) {
      fail("resume needs a session id, or --last. Try: claudekeeper list");
      return 2;
    }
    const prompt = args.prompt || args._.slice(2).join(" ").trim() || null;
    const opts = buildOpts(args, prompt || "Continue exactly where you left off.", id);
    const res = await supervise(opts);
    return res.ok ? 0 : 1;
  }

  fail(`unknown command "${cmd}". Try: claudekeeper help`);
  return 2;
}

// ---------------------------------------------------------------------------
// watch: transparently supervise a live interactive claude session
// ---------------------------------------------------------------------------

function cmdWatch(args) {
  const python = ENV.CLAUDEKEEPER_PYTHON || "python3";
  const script = path.join(__dirname, "keeper-watch.py");
  if (!fs.existsSync(script)) {
    fail(`could not find the supervisor at ${script}`);
    return Promise.resolve(1);
  }
  // Everything after -- is for claude; otherwise take the rest of the args.
  const claudeArgs = (args.passthrough && args.passthrough.length)
    ? args.passthrough
    : args._.slice(1);

  const env = Object.assign({}, process.env);
  if (args.model) env.CLAUDEKEEPER_PRIMARY_MODEL = args.model;
  if (args.fallback) env.CLAUDEKEEPER_FALLBACK_MODELS = args.fallback;
  if (args["no-switch"] || args["no-fallback"]) env.CLAUDEKEEPER_SWITCH = "0";
  if (args["no-wait"]) env.CLAUDEKEEPER_WAIT = "0";
  if (args["no-force-model"]) env.CLAUDEKEEPER_FORCE_MODEL = "0";
  if (args["dry-run"]) env.CLAUDEKEEPER_WATCH_DRYRUN = "1";
  if (args["continue-prompt"]) env.CLAUDEKEEPER_CONTINUE_PROMPT = args["continue-prompt"];

  return new Promise((resolve) => {
    const child = spawn(python, [script, ...claudeArgs], { stdio: "inherit", env });
    child.on("error", (err) => {
      fail(`could not launch ${python}: ${err.message}. Is Python 3 installed?`);
      resolve(1);
    });
    child.on("close", (code) => resolve(code == null ? 1 : code));
  });
}

// ---------------------------------------------------------------------------
// install-shell: wrap the claude command so regular sessions are supervised
// ---------------------------------------------------------------------------

const SHELL_MARKER_START = "# >>> claudekeeper >>>";
const SHELL_MARKER_END = "# <<< claudekeeper <<<";

function shellBlock() {
  return [
    SHELL_MARKER_START,
    "# Run the normal claude, kept alive across usage limits by claudekeeper.",
    "# Remove this block or run: claudekeeper uninstall-shell",
    'claude() { command claudekeeper watch -- "$@"; }',
    SHELL_MARKER_END,
    "",
  ].join("\n");
}

function defaultRcFile() {
  const shell = ENV.SHELL || "";
  if (shell.includes("zsh")) return path.join(os.homedir(), ".zshrc");
  if (shell.includes("bash")) return path.join(os.homedir(), ".bashrc");
  return null;
}

function cmdInstallShell(args) {
  const block = shellBlock();
  const target = args.file || defaultRcFile();

  if (!args.write) {
    process.stdout.write(
      `${c.bold("Add this to your shell profile to supervise every claude session:")}\n\n${block}\n`
    );
    if (target) {
      process.stdout.write(c.dim(`Detected profile: ${target}\n`));
      process.stdout.write(`Run ${c.bold("claudekeeper install-shell --write")} to add it there for you.\n`);
    } else {
      process.stdout.write(c.dim("Could not detect your shell profile. Add the block to the right file yourself.\n"));
    }
    return 0;
  }

  if (!target) {
    fail("could not detect your shell profile. Pass --file <path> to choose one.");
    return 2;
  }

  let existing = "";
  try {
    existing = fs.readFileSync(target, "utf8");
  } catch {
    existing = "";
  }
  if (existing.includes(SHELL_MARKER_START)) {
    log(`already installed in ${target}. Nothing to do.`);
    return 0;
  }
  const sep = existing && !existing.endsWith("\n") ? "\n" : "";
  fs.appendFileSync(target, `${sep}\n${block}`);
  log(c.green(`installed the claude wrapper in ${target}.`));
  log(`open a new shell, or run ${c.bold(`source ${target}`)}, then use ${c.bold("claude")} as usual.`);
  return 0;
}

function cmdUninstallShell(args) {
  const target = args.file || defaultRcFile();
  if (!target) {
    fail("could not detect your shell profile. Pass --file <path>.");
    return 2;
  }
  let existing;
  try {
    existing = fs.readFileSync(target, "utf8");
  } catch {
    log(`${target} not found. Nothing to remove.`);
    return 0;
  }
  if (!existing.includes(SHELL_MARKER_START)) {
    log(`no claudekeeper block found in ${target}.`);
    return 0;
  }
  const re = new RegExp(`\\n?${escapeRe(SHELL_MARKER_START)}[\\s\\S]*?${escapeRe(SHELL_MARKER_END)}\\n?`, "g");
  const next = existing.replace(re, "\n");
  fs.writeFileSync(target, next);
  log(c.green(`removed the claude wrapper from ${target}. Open a new shell to apply.`));
  return 0;
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function readPkg() {
  try {
    return JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  } catch {
    return {};
  }
}

function installSignalHandlers() {
  let interrupts = 0;
  process.on("SIGINT", () => {
    interrupts += 1;
    if (currentChild) {
      warn("interrupt received. Stopping the current run.");
      try {
        currentChild.kill("SIGINT");
      } catch {}
    }
    if (interrupts >= 2) {
      process.exit(130);
    }
    setTimeout(() => process.exit(130), 1500).unref();
  });
}

// Export the pure helpers so tests can exercise them without spawning claude.
module.exports = {
  classifyOutcome,
  parseResetEpochMs,
  normaliseEpoch,
  decideOnLimit,
  dedupeChain,
  parseArgs,
  buildOpts,
  formatDuration,
  splitList,
};

if (require.main === module) {
  main()
    .then((code) => process.exit(code || 0))
    .catch((err) => {
      fail(`unexpected error: ${err && err.stack ? err.stack : err}`);
      process.exit(1);
    });
}
