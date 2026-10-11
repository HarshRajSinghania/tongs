import assert from "node:assert/strict";
import test from "node:test";

import {
  DIFF_CONTEXT_STATUSES,
  GENERATE_DIFF_STATUSES,
  IPC_CHANNELS,
  MAX_DIFF_CONTEXT_LINES,
} from "../../../desktop/dist/src/shared/bridge.js";
import { assertParams, assertResult } from "../../../desktop/dist/src/main/security.js";

// The wire contract for #383: opt-in gap rows on diff.open, inline unchanged
// lines from diff.context and one-file snapshots from diff.generate. Every
// check asserts on plain values only.

const snapshot = "Ab_9-xY".padEnd(32, "z");
const revision = { head_sha: "c".repeat(40), base_sha: "a".repeat(40), start_sha: null };
const fileRow = {
  kind: "file", file_index: 1, old_path: "big.py", new_path: "big.py", status: "modified",
  additions: 1, deletions: 1, is_binary: false, language: "python", is_truncated: false,
  is_empty: false, is_mode_only: false, is_rename_only: false, is_unavailable: false,
  old_mode: null, new_mode: null,
};
const hunkRow = {
  kind: "hunk", file_index: 1, hunk_index: 0, header: "@@ -4,7 +4,7 @@", old_start: 4,
  old_count: 7, new_start: 4, new_count: 7, context_text: "",
};
const lineRow = {
  kind: "line", file_index: 1, hunk_index: 0, old_line: 4, new_line: 4, content: "x", line_type: "context",
};
const gapRow = {
  kind: "gap", file_index: 0, gap_index: 1, position: "between", old_start: 14, new_start: 15, count: 13,
};
const contextRow = { kind: "context", file_index: 0, old_line: 14, new_line: 15, content: "line 14" };

function page(entries, extra = {}) {
  return { snapshot_id: snapshot, resource: "review", revision, cursor: 0, next_cursor: null, entries, ...extra };
}
function contextResult(extra = {}) {
  return { status: "ok", message: null, line_count: 41, rows: [contextRow], ...extra };
}
function generated(extra = {}) {
  return { status: "ok", message: null, generated: true, ...page([fileRow, hunkRow, lineRow]), ...extra };
}
function refused(status, extra = {}) {
  return {
    status, message: "Not this time.", generated: false, snapshot_id: null, resource: null,
    revision: null, cursor: null, next_cursor: null, entries: [], ...extra,
  };
}
function rejects(method, value, label) {
  assert.throws(() => assertResult(method, value), new RegExp(`Invalid ${method.replace(".", "\\.")} result`), label);
}

test("the bridge names one channel per new method", () => {
  assert.equal(IPC_CHANNELS.getDiffContext, "tongs:diff.context");
  assert.equal(IPC_CHANNELS.generateDiff, "tongs:diff.generate");
  assert.equal(MAX_DIFF_CONTEXT_LINES, 1000);
  assert.deepEqual([...DIFF_CONTEXT_STATUSES], ["ok", "too_large", "binary", "not_found", "mismatch", "unsupported", "expired"]);
  assert.deepEqual([...GENERATE_DIFF_STATUSES], ["ok", "too_large", "binary", "not_found", "unsupported", "not_truncated", "no_merge_base", "expired"]);
});

test("diff.open takes an optional boolean gaps flag", () => {
  assertParams("diff.open", { review: "review", gaps: true });
  assertParams("diff.open", { review: "review", layout: "split", gaps: false, from_snapshot: snapshot });
  for (const gaps of ["true", 1, 0, null, [], {}]) {
    assert.throws(() => assertParams("diff.open", { review: "review", gaps }), /Invalid desktop boolean parameter/, JSON.stringify(gaps));
  }
  // The flag belongs to diff.open only.
  assert.throws(() => assertParams("diff.page", { snapshot: "s", resource: "r", cursor: 0, gaps: true }), /Invalid desktop parameter fields/);
});

test("diff.context takes exactly a review, snapshot, file index, start and count", () => {
  const valid = { review: "review", snapshot, file_index: 0, new_start: 1, count: MAX_DIFF_CONTEXT_LINES };
  assertParams("diff.context", valid);
  assertParams("diff.context", { ...valid, file_index: 100_000, new_start: 1_000_000, count: 1 });
  for (const key of Object.keys(valid)) {
    const missing = { ...valid };
    delete missing[key];
    assert.throws(() => assertParams("diff.context", missing), /Invalid desktop parameter fields/, key);
  }
  for (const extra of [{ path: "app.py" }, { head_sha: "c".repeat(40) }, { layout: "split" }, { gaps: true }]) {
    assert.throws(() => assertParams("diff.context", { ...valid, ...extra }), /Invalid desktop parameter fields/, JSON.stringify(extra));
  }
  const bad = [
    { count: 0 }, { count: 1001 }, { count: 1.5 }, { count: "10" }, { count: true },
    { file_index: -1 }, { file_index: 100_001 }, { file_index: null },
    { new_start: 0 }, { new_start: -4 }, { new_start: 1_000_001 }, { new_start: Number.MAX_SAFE_INTEGER + 2 },
  ];
  for (const change of bad) {
    assert.throws(() => assertParams("diff.context", { ...valid, ...change }), /Invalid desktop numeric parameter/, JSON.stringify(change));
  }
  for (const value of ["", "x".repeat(65), "snap shot", "../s", 7, null]) {
    assert.throws(() => assertParams("diff.context", { ...valid, snapshot: value }), /Invalid desktop snapshot parameter/, JSON.stringify(value));
  }
});

test("diff.generate takes exactly a review, snapshot and file index", () => {
  const valid = { review: "review", snapshot, file_index: 2 };
  assertParams("diff.generate", valid);
  for (const extra of [{ count: 1 }, { new_start: 1 }, { path: "big.py" }]) {
    assert.throws(() => assertParams("diff.generate", { ...valid, ...extra }), /Invalid desktop parameter fields/, JSON.stringify(extra));
  }
  for (const key of Object.keys(valid)) {
    const missing = { ...valid };
    delete missing[key];
    assert.throws(() => assertParams("diff.generate", missing), /Invalid desktop parameter fields/, key);
  }
  for (const file_index of [-1, 100_001, 0.5, "1"]) {
    assert.throws(() => assertParams("diff.generate", { ...valid, file_index }), /Invalid desktop numeric parameter/, JSON.stringify(file_index));
  }
  assert.throws(() => assertParams("diff.generate", { ...valid, snapshot: "a/b" }), /Invalid desktop snapshot parameter/);
});

test("diff rows accept gap rows with exact keys and bounded numbers", () => {
  const after = { ...gapRow, gap_index: 2, position: "after", old_start: 34, new_start: 35, count: null };
  const before = { ...gapRow, gap_index: 0, position: "before", old_start: 1, new_start: 1, count: 6 };
  for (const method of ["diff.open", "diff.page"]) {
    assertResult(method, page([{ ...fileRow, file_index: 0 }, before, hunkRow, gapRow, after]));
  }
  const bad = [
    { ...gapRow, extra: 1 },
    (({ count, ...rest }) => rest)(gapRow),
    { ...gapRow, kind: "gaps" },
    { ...gapRow, position: "inside" },
    { ...gapRow, count: null },
    { ...gapRow, count: 0 },
    { ...gapRow, count: -2 },
    { ...gapRow, old_start: 0 },
    { ...gapRow, new_start: 1_000_001 },
    { ...gapRow, gap_index: -1 },
    { ...gapRow, file_index: 1.5 },
    { ...gapRow, count: "13" },
  ];
  for (const row of bad) rejects("diff.page", page([row]), JSON.stringify(row));
});

test("diff.context results are validated by status", () => {
  assertResult("diff.context", contextResult());
  assertResult("diff.context", contextResult({ rows: [] }));
  assertResult("diff.context", contextResult({
    rows: [contextRow, { ...contextRow, old_line: 15, new_line: 16, content: "line 15" }],
  }));
  for (const status of DIFF_CONTEXT_STATUSES.filter((value) => value !== "ok")) {
    assertResult("diff.context", { status, message: "Refresh the review.", line_count: null, rows: [] });
  }
  const bad = [
    { ...contextResult(), extra: true },
    (({ line_count, ...rest }) => rest)(contextResult()),
    contextResult({ status: "maybe" }),
    contextResult({ message: "ok" }),
    contextResult({ line_count: null }),
    contextResult({ line_count: -1 }),
    contextResult({ rows: [{ ...contextRow, kind: "line" }] }),
    contextResult({ rows: [{ ...contextRow, extra: 1 }] }),
    contextResult({ rows: [{ ...contextRow, old_line: 0 }] }),
    contextResult({ rows: [{ ...contextRow, new_line: -1 }] }),
    contextResult({ rows: [{ ...contextRow, new_line: 42 }] }),
    contextResult({ rows: [{ ...contextRow, content: null }] }),
    contextResult({ rows: [{ ...contextRow, file_index: -1 }] }),
    contextResult({ rows: [contextRow, { ...contextRow, new_line: 17 }] }),
    contextResult({ rows: [contextRow, { ...contextRow, file_index: 1, new_line: 16 }] }),
    contextResult({ rows: Array.from({ length: 1001 }, (_, index) => ({ ...contextRow, new_line: index + 1 })), line_count: 1001 }),
    contextResult({ rows: [{ ...contextRow, content: "x".repeat(4 * 1024 * 1024 + 1) }] }),
    { status: "too_large", message: null, line_count: null, rows: [] },
    { status: "binary", message: "Binary.", line_count: 3, rows: [] },
    { status: "expired", message: "Reload.", line_count: null, rows: [contextRow] },
  ];
  for (const value of bad) rejects("diff.context", value, JSON.stringify(value).slice(0, 120));
});

test("diff.generate results are validated by status", () => {
  assertResult("diff.generate", generated());
  assertResult("diff.generate", generated({ next_cursor: 3 }));
  for (const status of GENERATE_DIFF_STATUSES.filter((value) => value !== "ok")) {
    assertResult("diff.generate", refused(status));
  }
  const bad = [
    { ...generated(), extra: 1 },
    (({ generated: _flag, ...rest }) => rest)(generated()),
    generated({ status: "done" }),
    generated({ generated: false }),
    generated({ message: "Generated." }),
    generated({ cursor: 2 }),
    generated({ next_cursor: -1 }),
    generated({ entries: [hunkRow, lineRow] }),
    generated({ entries: [fileRow, { ...gapRow, file_index: 1 }] }),
    generated({ entries: [fileRow, { ...lineRow, file_index: 2 }] }),
    generated({ entries: [fileRow, { ...lineRow, kind: "context" }] }),
    generated({ revision: null }),
    refused("ok"),
    refused("too_large", { generated: true }),
    refused("too_large", { message: null }),
    refused("binary", { snapshot_id: snapshot }),
    refused("binary", { entries: [fileRow] }),
    refused("unknown"),
  ];
  for (const value of bad) rejects("diff.generate", value, JSON.stringify(value).slice(0, 120));
});
