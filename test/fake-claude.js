#!/usr/bin/env node
"use strict";

/*
 * A stand in for the real claude binary, used only by the integration test.
 * It reads --model and emits stream-json events that mimic either a usage limit
 * or a clean success, so the supervisor's switch and wait logic can be exercised
 * without spending any quota.
 *
 * Behaviour is driven by env:
 *   FAKE_MODE=limit_models   models in FAKE_LIMIT_MODELS emit a usage limit
 *   FAKE_MODE=limit_once     every model limits on its first call, then succeeds
 *   FAKE_LIMIT_MODELS        comma list, used by limit_models mode
 *   FAKE_RESET_OFFSET_S      seconds from now to report as the reset time
 *   FAKE_STATE_DIR           directory used to count calls per model
 *   FAKE_SESSION_ID          session id to report in the init event
 */

const fs = require("node:fs");
const path = require("node:path");

const argv = process.argv.slice(2);
let model = "unknown";
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === "--model") model = argv[i + 1];
}

const sessionId = process.env.FAKE_SESSION_ID || "fake-session-0001";

// Drain stdin (the prompt) so the parent's stdin.end resolves cleanly.
try {
  fs.readFileSync(0);
} catch {}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

emit({ type: "system", subtype: "init", session_id: sessionId, model });

function callCount(m) {
  const dir = process.env.FAKE_STATE_DIR;
  if (!dir) return 1;
  const f = path.join(dir, `count-${m}`);
  let n = 0;
  try {
    n = parseInt(fs.readFileSync(f, "utf8"), 10) || 0;
  } catch {}
  n += 1;
  fs.writeFileSync(f, String(n));
  return n;
}

function shouldLimit() {
  const mode = process.env.FAKE_MODE || "limit_models";
  if (mode === "limit_once") {
    return callCount(model) === 1;
  }
  const list = (process.env.FAKE_LIMIT_MODELS || "").split(",").map((s) => s.trim());
  return list.includes(model);
}

if (shouldLimit()) {
  const offset = parseInt(process.env.FAKE_RESET_OFFSET_S || "0", 10);
  const resetEpoch = Math.floor(Date.now() / 1000) + (offset || 3600);
  emit({
    type: "result",
    subtype: "error_during_execution",
    is_error: true,
    session_id: sessionId,
    result: `Claude AI usage limit reached|${resetEpoch}`,
  });
  process.exit(1);
} else {
  emit({
    type: "assistant",
    session_id: sessionId,
    message: { role: "assistant", content: [{ type: "text", text: `ok from ${model}` }] },
  });
  emit({
    type: "result",
    subtype: "success",
    is_error: false,
    session_id: sessionId,
    result: `finished on ${model}`,
  });
  process.exit(0);
}
