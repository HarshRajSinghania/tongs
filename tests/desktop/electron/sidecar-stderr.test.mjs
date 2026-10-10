// The service's stderr diagnostics reach the main-process log, bounded per
// line and per session. Assertions use strings, counts and booleans only.
import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_STDERR_LINE_BYTES,
  MAX_STDERR_SESSION_BYTES,
  STDERR_LINE_TRUNCATED,
  STDERR_LOG_PREFIX,
  STDERR_OUTPUT_DROPPED,
} from "../../../desktop/dist/src/main/sidecar.js";
import { harness, transportFor } from "./fake-sidecar.mjs";

async function drain(rounds = 4) {
  for (let round = 0; round < rounds; round += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function logged(stderr, after) {
  const lines = [];
  const fake = harness({ stderr });
  const transport = transportFor(fake, 1_000, (line) => lines.push(line));
  await transport.start();
  await drain();
  if (after) await after(fake.children[0]);
  await transport.stop();
  return lines;
}

test("a diagnostic line the service writes reaches the main-process log", async () => {
  const message =
    "Desktop operation reviews.list failed: RuntimeError: token [REDACTED] rejected";
  const lines = await logged([`${message}\n`]);
  assert.deepEqual(lines, [`${STDERR_LOG_PREFIX}${message}`]);
  assert.equal(STDERR_LOG_PREFIX.length > 0, true);
});

test("lines are split, joined across chunks, and blank lines are skipped", async () => {
  const lines = await logged(["first li", "ne\r\n\n  \nsecond\nthi", "rd"], async (child) => {
    // A last line without a newline is forwarded when the stream ends.
    child.stderr.end();
    await drain();
  });
  assert.deepEqual(lines, [
    `${STDERR_LOG_PREFIX}first line`,
    `${STDERR_LOG_PREFIX}second`,
    `${STDERR_LOG_PREFIX}third`,
  ]);
});

test("a very long line is cut at the line bound with a marker", async () => {
  const lines = await logged([`${"x".repeat(MAX_STDERR_LINE_BYTES * 3)}\nafter\n`]);
  assert.equal(lines.length, 2);
  assert.equal(
    lines[0],
    `${STDERR_LOG_PREFIX}${"x".repeat(MAX_STDERR_LINE_BYTES)}${STDERR_LINE_TRUNCATED}`,
  );
  assert.equal(lines[1], `${STDERR_LOG_PREFIX}after`);
});

test("control characters cannot reach the log as terminal escapes", async () => {
  const lines = await logged(["\u001b[31mred\u001b[0m\tdone\n"]);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].includes("\u001b"), false);
  assert.equal(lines[0].endsWith("[31mred�[0m\tdone"), true);
});

test("C1 controls, separators and bidi overrides are replaced too", async () => {
  const unsafe = ["\u009b", "\u0085", " ", " ", "‮", "⁦", "⁩"];
  const lines = await logged([`a\u009b31mb\u0085c d e‮f⁦g⁩h\n`]);
  assert.equal(lines.length, 1);
  assert.equal(
    unsafe.some((character) => lines[0].includes(character)),
    false,
  );
  assert.equal(lines[0], `${STDERR_LOG_PREFIX}a�31mb�c�d�e�f�g�h`);
});

test("chatty output stops at the session bound with one dropped-output line", async () => {
  const line = `${"y".repeat(1023)}\n`;
  const chunk = line.repeat(256);
  // Twice the session bound, written in 256 KiB chunks.
  const chunks = Array.from(
    { length: (2 * MAX_STDERR_SESSION_BYTES) / chunk.length },
    () => chunk,
  );
  const lines = await logged(chunks, async (child) => {
    child.stderr.write("late line\n");
    await drain();
  });
  const dropped = lines.filter((item) => item === `${STDERR_LOG_PREFIX}${STDERR_OUTPUT_DROPPED}`);
  assert.equal(dropped.length, 1);
  assert.equal(lines.at(-1), `${STDERR_LOG_PREFIX}${STDERR_OUTPUT_DROPPED}`);
  assert.equal(lines.length, MAX_STDERR_SESSION_BYTES / line.length + 1);
  assert.equal(lines.includes(`${STDERR_LOG_PREFIX}late line`), false);
});

test("each service session gets its own diagnostic budget", async () => {
  const lines = [];
  const fake = harness({
    stderr: (index) =>
      index === 0
        ? [`${"z".repeat(1023)}\n`.repeat(1100)]
        : ["restarted session line\n"],
  });
  const transport = transportFor(fake, 1_000, (line) => lines.push(line));
  await transport.start();
  await drain();
  await transport.restart();
  await drain();
  await transport.stop();
  assert.equal(lines.includes(`${STDERR_LOG_PREFIX}${STDERR_OUTPUT_DROPPED}`), true);
  assert.equal(lines.at(-1), `${STDERR_LOG_PREFIX}restarted session line`);
});

test("without an injected sink diagnostics go to console.error", async () => {
  const original = console.error;
  const errors = [];
  console.error = (...values) => errors.push(values.map(String).join(" "));
  try {
    const fake = harness({ stderr: ["default sink line\n"] });
    const transport = transportFor(fake);
    await transport.start();
    await drain();
    await transport.stop();
  } finally {
    console.error = original;
  }
  assert.deepEqual(errors, [`${STDERR_LOG_PREFIX}default sink line`]);
});
