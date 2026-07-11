"use strict";

/*
 * Unit tests for the pure logic: limit detection, reset time parsing, and the
 * model switch state machine. These run without spawning claude, so they use no
 * quota. Run with: node test/run-tests.js
 */

const assert = require("node:assert");
const k = require("../bin/claudekeeper.js");

let passed = 0;
let failed = 0;

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

// --- classifyOutcome -------------------------------------------------------

test("clean success is ok", () => {
  const o = k.classifyOutcome({ combined: "", code: 0, resultText: "done", isError: false });
  assert.strictEqual(o.kind, "ok");
});

test("usage limit in result text is a limit", () => {
  const o = k.classifyOutcome({
    combined: '{"type":"result","is_error":true,"result":"Claude AI usage limit reached"}',
    code: 1,
    resultText: "Claude AI usage limit reached",
    isError: true,
  });
  assert.strictEqual(o.kind, "limit");
});

test("usage limit in stderr is a limit even on exit 0", () => {
  const o = k.classifyOutcome({
    combined: "API Error: usage limit reached",
    code: 0,
    resultText: "",
    isError: false,
  });
  assert.strictEqual(o.kind, "limit");
});

test("credit balance too low is fatal, not a limit", () => {
  const o = k.classifyOutcome({
    combined: "Your credit balance is too low to access the API",
    code: 1,
    resultText: "",
    isError: true,
  });
  assert.strictEqual(o.kind, "fatal");
});

test("invalid api key is fatal", () => {
  const o = k.classifyOutcome({ combined: "Invalid API key", code: 1, resultText: "", isError: true });
  assert.strictEqual(o.kind, "fatal");
});

test("nonzero exit with no signal is unknown", () => {
  const o = k.classifyOutcome({ combined: "segfault", code: 139, resultText: "", isError: false });
  assert.strictEqual(o.kind, "unknown");
});

test("exit 0 flagged error without limit is still ok but noted", () => {
  const o = k.classifyOutcome({ combined: "", code: 0, resultText: "", isError: true });
  assert.strictEqual(o.kind, "ok");
  assert.strictEqual(o.flaggedError, true);
});

// --- parseResetEpochMs -----------------------------------------------------

test("pipe format seconds", () => {
  const ms = k.parseResetEpochMs("Claude AI usage limit reached|1712345678");
  assert.strictEqual(ms, 1712345678 * 1000);
});

test("pipe format millis", () => {
  const ms = k.parseResetEpochMs("usage limit reached|1712345678000");
  assert.strictEqual(ms, 1712345678000);
});

test("numeric resetsAt field", () => {
  const ms = k.parseResetEpochMs('{"limit":{"resetsAt":1712345678}}');
  assert.strictEqual(ms, 1712345678 * 1000);
});

test("reset_at snake case field", () => {
  const ms = k.parseResetEpochMs('"reset_at": 1712345678');
  assert.strictEqual(ms, 1712345678 * 1000);
});

test("iso resetsAt field", () => {
  const ms = k.parseResetEpochMs('"resetsAt":"2026-07-10T18:00:00Z"');
  assert.strictEqual(ms, Date.parse("2026-07-10T18:00:00Z"));
});

test("human phrase resets at 3pm returns a future time", () => {
  const ms = k.parseResetEpochMs("Your limit will reset at 3pm");
  assert.ok(ms > Date.now(), "should be in the future");
  const d = new Date(ms);
  assert.strictEqual(d.getHours(), 15);
});

test("no reset info returns null", () => {
  assert.strictEqual(k.parseResetEpochMs("usage limit reached, sorry"), null);
});

test("normaliseEpoch handles seconds and millis", () => {
  assert.strictEqual(k.normaliseEpoch("1712345678"), 1712345678000);
  assert.strictEqual(k.normaliseEpoch("1712345678000"), 1712345678000);
});

// --- decideOnLimit ---------------------------------------------------------

test("fable limit with opus fallback switches", () => {
  const d = k.decideOnLimit({ modelIndex: 0, chainLength: 2, switchEnabled: true, waitEnabled: true });
  assert.strictEqual(d.action, "switch");
  assert.strictEqual(d.nextIndex, 1);
});

test("last model limited waits", () => {
  const d = k.decideOnLimit({ modelIndex: 1, chainLength: 2, switchEnabled: true, waitEnabled: true });
  assert.strictEqual(d.action, "wait");
  assert.strictEqual(d.nextIndex, 0);
});

test("switch disabled means wait even with fallback left", () => {
  const d = k.decideOnLimit({ modelIndex: 0, chainLength: 2, switchEnabled: false, waitEnabled: true });
  assert.strictEqual(d.action, "wait");
});

test("no switch and no wait stops", () => {
  const d = k.decideOnLimit({ modelIndex: 1, chainLength: 2, switchEnabled: true, waitEnabled: false });
  assert.strictEqual(d.action, "stop");
});

// --- dedupeChain -----------------------------------------------------------

test("dedupeChain collapses duplicate opus primary and fallback", () => {
  assert.deepStrictEqual(k.dedupeChain(["opus", "opus"]), ["opus"]);
});

test("dedupeChain keeps a distinct chain in order", () => {
  assert.deepStrictEqual(k.dedupeChain(["sonnet", "opus"]), ["sonnet", "opus"]);
});

test("dedupeChain is case insensitive and drops empties", () => {
  assert.deepStrictEqual(k.dedupeChain(["fable", "", "Fable", "opus"]), ["fable", "opus"]);
});

// --- parseArgs and buildOpts ----------------------------------------------

test("parseArgs collects passthrough after double dash", () => {
  const a = k.parseArgs(["run", "hello", "--model", "fable", "--", "--foo", "bar"]);
  assert.deepStrictEqual(a._, ["run", "hello"]);
  assert.strictEqual(a.model, "fable");
  assert.deepStrictEqual(a.passthrough, ["--foo", "bar"]);
});

test("buildOpts defaults primary fable and fallback opus", () => {
  const a = k.parseArgs(["run", "x"]);
  const o = k.buildOpts(a, "x", null);
  assert.strictEqual(o.primaryModel, "fable");
  assert.deepStrictEqual(o.fallbackModels, ["opus"]);
});

test("buildOpts --no-fallback empties the chain", () => {
  const a = k.parseArgs(["run", "x", "--no-fallback"]);
  const o = k.buildOpts(a, "x", null);
  assert.deepStrictEqual(o.fallbackModels, []);
});

test("buildOpts custom fallback chain", () => {
  const a = k.parseArgs(["run", "x", "--fallback", "opus,sonnet"]);
  const o = k.buildOpts(a, "x", null);
  assert.deepStrictEqual(o.fallbackModels, ["opus", "sonnet"]);
});

// --- formatDuration --------------------------------------------------------

test("formatDuration is readable", () => {
  assert.strictEqual(k.formatDuration(45 * 1000), "45s");
  assert.strictEqual(k.formatDuration(90 * 1000), "1m 30s");
  assert.strictEqual(k.formatDuration(3 * 3600 * 1000), "3h 0m");
});

// --- summary ---------------------------------------------------------------

process.stdout.write(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
