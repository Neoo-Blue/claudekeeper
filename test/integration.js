"use strict";

/*
 * Integration tests that drive the real supervisor loop against a fake claude,
 * so the switch and wait behaviour is verified without spending quota.
 * Run with: node test/integration.js
 */

const assert = require("node:assert");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const CLI = path.join(__dirname, "..", "bin", "claudekeeper.js");
// The fake claude has a node shebang and is executable, so it can be spawned
// directly as a single program, exactly like the real claude binary.
const FAKE_BIN = path.join(__dirname, "fake-claude.js");

let passed = 0;
let failed = 0;

function run(args, env) {
  return spawnSync(process.execPath, [CLI, ...args], {
    encoding: "utf8",
    env: Object.assign({}, process.env, { CLAUDEKEEPER_CLAUDE_BIN: FAKE_BIN, NO_COLOR: "1" }, env),
    timeout: 30000,
  });
}

function test(name, fn) {
  try {
    fn();
    passed += 1;
    process.stdout.write(`  ok   ${name}\n`);
  } catch (err) {
    failed += 1;
    process.stdout.write(`  FAIL ${name}\n       ${err.message}\n`);
  }
}

function tmpStateDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "keeper-it-"));
}

// 1) Fable limit switches to opus and finishes.
test("fable limit switches to opus and succeeds", () => {
  const r = run(
    ["run", "do the thing", "--model", "fable", "--fallback", "opus", "--quiet"],
    { FAKE_MODE: "limit_models", FAKE_LIMIT_MODELS: "fable" }
  );
  const out = r.stdout + r.stderr;
  assert.strictEqual(r.status, 0, `expected exit 0, got ${r.status}\n${out}`);
  assert.match(out, /Switching to opus/i, out);
  assert.match(out, /done/i, out);
});

// 2) Every model limited with waiting disabled stops with a nonzero exit.
test("all models limited and no wait stops nonzero", () => {
  const r = run(
    ["run", "do the thing", "--model", "fable", "--fallback", "opus", "--no-wait", "--quiet"],
    { FAKE_MODE: "limit_models", FAKE_LIMIT_MODELS: "fable,opus" }
  );
  const out = r.stdout + r.stderr;
  assert.notStrictEqual(r.status, 0, `expected nonzero exit\n${out}`);
});

// 3) Wait path: both limit once, short reset, then resume on primary succeeds.
test("wait for reset then resume succeeds", () => {
  const dir = tmpStateDir();
  const r = run(
    [
      "run", "do the thing",
      "--model", "fable", "--fallback", "opus",
      "--quiet",
    ],
    {
      FAKE_MODE: "limit_once",
      FAKE_RESET_OFFSET_S: "1",
      FAKE_STATE_DIR: dir,
      CLAUDEKEEPER_RESET_BUFFER_SECONDS: "0",
    }
  );
  const out = r.stdout + r.stderr;
  assert.strictEqual(r.status, 0, `expected exit 0, got ${r.status}\n${out}`);
  assert.match(out, /Waiting|waiting/, out);
  assert.match(out, /Resuming on the primary/i, out);
  assert.match(out, /done/i, out);
});

// 4) No switch means it waits even when a fallback exists.
test("no switch waits instead of switching", () => {
  const dir = tmpStateDir();
  const r = run(
    ["run", "do the thing", "--model", "fable", "--fallback", "opus", "--no-switch", "--quiet"],
    {
      FAKE_MODE: "limit_once",
      FAKE_RESET_OFFSET_S: "1",
      FAKE_STATE_DIR: dir,
      CLAUDEKEEPER_RESET_BUFFER_SECONDS: "0",
    }
  );
  const out = r.stdout + r.stderr;
  assert.strictEqual(r.status, 0, `expected exit 0, got ${r.status}\n${out}`);
  assert.doesNotMatch(out, /Switching to/i, out);
  assert.match(out, /Waiting|waiting/, out);
});

// 5) Primary opus with the default opus fallback: the chain dedupes, so a limit
//    waits for reset rather than wasting a switch to the same model.
test("opus primary dedupes and waits, no wasted switch", () => {
  const dir = tmpStateDir();
  const r = run(
    ["run", "do the thing", "--model", "opus", "--quiet"],
    {
      FAKE_MODE: "limit_once",
      FAKE_RESET_OFFSET_S: "1",
      FAKE_STATE_DIR: dir,
      CLAUDEKEEPER_RESET_BUFFER_SECONDS: "0",
    }
  );
  const out = r.stdout + r.stderr;
  assert.strictEqual(r.status, 0, `expected exit 0, got ${r.status}\n${out}`);
  assert.doesNotMatch(out, /Switching to/i, out);
  assert.match(out, /Waiting|waiting/, out);
  assert.match(out, /done/i, out);
});

process.stdout.write(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
