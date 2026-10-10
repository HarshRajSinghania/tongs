import assert from "node:assert/strict";
import test from "node:test";

import {
  assertReviewBoundResult,
  assertReviewResult,
} from "../../../desktop/dist/src/main/review.js";

const revision = { head_sha: "head", base_sha: "base", start_sha: null };
const review = "opaque-review";

function draft(id, body, comments = []) {
  return {
    id,
    review,
    revision,
    version: 1,
    body,
    verdict: null,
    comments,
    state: "editable",
    created_at: "2026-10-10T00:00:00+00:00",
    updated_at: "2026-10-10T00:00:00+00:00",
  };
}

function general(id, body) {
  return { id, kind: "general", body };
}

const first = "11111111-1111-4111-8111-111111111111";
const oversized = "22222222-2222-4222-8222-222222222222";
const third = "33333333-3333-4333-8333-333333333333";
const tooMany = "44444444-4444-4444-8444-444444444444";
const secret = "terminal draft text ".repeat(3_500);

function captureWarnings(run) {
  const original = console.warn;
  const lines = [];
  console.warn = (...args) => lines.push(args.map(String).join(" "));
  try {
    run();
  } finally {
    console.warn = original;
  }
  return lines;
}

test("an oversized stored draft is skipped and the other drafts still list", () => {
  const result = {
    cursor: 0,
    next_cursor: null,
    drafts: [
      draft(first, "Summary"),
      draft(oversized, secret),
      draft(third, "", [general("55555555-5555-4555-8555-555555555555", "Note")]),
    ],
  };

  const lines = captureWarnings(() =>
    assertReviewBoundResult("drafts.list", { review }, result),
  );

  assert.deepEqual(Object.keys(result).sort(), ["cursor", "drafts", "next_cursor"]);
  assert.equal(result.drafts.length, 2);
  assert.deepEqual(
    result.drafts.map((item) => item.id),
    [first, third],
  );
  assert.equal(result.cursor, 0);
  assert.equal(result.next_cursor, null);
  assert.equal(lines.length, 1);
  assert.equal(
    lines[0],
    "Review draft list: skipped 1 stored draft(s) the desktop cannot show.",
  );
  assert.equal(lines[0].includes("terminal draft text"), false);
  assert.equal(lines[0].includes(oversized), false);
});

test("drafts over the comment limit or with control characters are counted once", () => {
  const comments = Array.from({ length: 201 }, (_, index) =>
    general(`66666666-6666-4666-8666-${String(index).padStart(12, "0")}`, "Note"),
  );
  const result = {
    cursor: 0,
    next_cursor: null,
    drafts: [
      draft(tooMany, "", comments),
      draft(first, "bell\u0007"),
      draft(third, "tab\tand\nnewline"),
    ],
  };

  const lines = captureWarnings(() => assertReviewResult("drafts.list", result));

  assert.deepEqual(
    result.drafts.map((item) => item.id),
    [third],
  );
  assert.equal(lines.length, 1);
  assert.equal(lines[0].includes("skipped 2 stored draft(s)"), true);
});

test("a page of valid drafts is unchanged and logs nothing", () => {
  const result = {
    cursor: 0,
    next_cursor: null,
    drafts: [draft(first, "x".repeat(65_536)), draft(third, "Summary")],
  };
  const before = JSON.stringify(result);

  const lines = captureWarnings(() => assertReviewResult("drafts.list", result));

  assert.equal(JSON.stringify(result), before);
  assert.equal(lines.length, 0);
});

test("the page itself is still validated as a whole", () => {
  const lines = captureWarnings(() => {
    assert.throws(
      () => assertReviewResult("drafts.list", { cursor: 0, next_cursor: null }),
      /Invalid review fields/,
    );
    assert.throws(
      () =>
        assertReviewResult("drafts.list", {
          cursor: 0,
          next_cursor: null,
          drafts: Array.from({ length: 101 }, () => draft(first, "Summary")),
        }),
      /Invalid review array/,
    );
    assert.throws(
      () =>
        assertReviewBoundResult(
          "drafts.list",
          { review },
          { cursor: 0, next_cursor: null, drafts: [{ ...draft(first, "Summary"), review: "other-review" }] },
        ),
      /Listed review draft/,
    );
  });
  assert.equal(lines.length, 0);
});
