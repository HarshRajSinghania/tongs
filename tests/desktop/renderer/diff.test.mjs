import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  CHANGES_OVERFLOW_NOTICE,
  createDiffFeature,
  loadAllPages,
  resolveDiscussionTarget,
} from "../../../desktop/dist/src/renderer/features/diff/index.js";
import { clearPendingEdit } from "../../../desktop/dist/src/renderer/features/review/drawer.js";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://app.invalid/",
});
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
});
const { cleanup, render, waitFor } = desktopRequire("@testing-library/react");
const React = desktopRequire("react");
afterEach(() => {
  cleanup();
  clearPendingEdit();
});

const revision = { head_sha: "head", base_sha: "base", start_sha: null };

test("diff reconstruction follows next_cursor across page boundaries", async () => {
  const calls = [];
  const bridge = {
    cancelRead: async () => true,
    openDiff: (params) => {
      calls.push(["open", params]);
      return read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 9,
        entries: [
          {
            kind: "file",
            file_index: 0,
            old_path: "a.py",
            new_path: "a.py",
            status: "modified",
            additions: 1,
            deletions: 1,
            is_binary: false,
            language: "python",
            is_truncated: false,
            is_empty: false,
            is_mode_only: false,
            is_rename_only: false,
            is_unavailable: false,
          },
        ],
      });
    },
    pageDiff: (params) => {
      calls.push(["page", params]);
      return read("page", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 9,
        next_cursor: null,
        entries: [
          {
            kind: "hunk",
            file_index: 0,
            hunk_index: 0,
            header: "@@ -1 +1 @@",
            old_start: 1,
            old_count: 1,
            new_start: 1,
            new_count: 1,
            context_text: "",
          },
        ],
      });
    },
  };
  const result = await loadAllPages(
    "review",
    "split",
    bridge,
    new QueryCoordinator(bridge),
  );
  assert.equal(result.layout, "split");
  assert.equal(result.rows.length, 2);
  assert.deepEqual(calls, [
    ["open", { review: "review", layout: "split", max_items: 1000 }],
    [
      "page",
      { snapshot: "s1", resource: "review", cursor: 9, max_items: 1000 },
    ],
  ]);
});

test("diff reconstruction rejects a revision change between pages", async () => {
  const bridge = {
    cancelRead: async () => true,
    openDiff: () =>
      read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 1,
        entries: [],
      }),
    pageDiff: () =>
      read("page", {
        snapshot_id: "s1",
        resource: "review",
        revision: { ...revision, head_sha: "changed" },
        cursor: 1,
        next_cursor: null,
        entries: [],
      }),
  };
  await assert.rejects(
    loadAllPages("review", "unified", bridge, new QueryCoordinator(bridge)),
    (error) => error.code === "revision_changed" && error.retryable === false,
  );
});

test("diff reconstruction rejects nonprogressing and mismatched page cursors", async () => {
  for (const page of [
    {
      snapshot_id: "s1",
      resource: "review",
      revision,
      cursor: 1,
      next_cursor: 1,
      entries: [],
    },
    {
      snapshot_id: "s1",
      resource: "review",
      revision,
      cursor: 2,
      next_cursor: null,
      entries: [],
    },
  ]) {
    let calls = 0;
    const bridge = {
      cancelRead: async () => true,
      openDiff: () =>
        read("open", {
          snapshot_id: "s1",
          resource: "review",
          revision,
          cursor: 0,
          next_cursor: 1,
          entries: [],
        }),
      pageDiff: () => {
        calls += 1;
        return read("page", page);
      },
    };
    await assert.rejects(
      loadAllPages("review", "unified", bridge, new QueryCoordinator(bridge)),
      (error) => error.code === "invalid_diff_page",
    );
    assert.equal(calls, 1);
  }
});

test("diff reconstruction rejects a backward first cursor", async () => {
  const bridge = {
    cancelRead: async () => true,
    openDiff: () =>
      read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 0,
        entries: [],
      }),
  };
  await assert.rejects(
    loadAllPages("review", "unified", bridge, new QueryCoordinator(bridge)),
    (error) => error.code === "invalid_diff_page",
  );
});

test("diff reconstruction retains a bounded partial result at the row cap", async () => {
  const row = {
    kind: "line",
    file_index: 0,
    hunk_index: 0,
    old_line: 1,
    new_line: 1,
    content: "context",
    line_type: "context",
  };
  let pageCalls = 0;
  const bridge = {
    cancelRead: async () => true,
    openDiff: () =>
      read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 1000,
        entries: Array(100_000).fill(row),
      }),
    pageDiff: () => {
      pageCalls += 1;
      throw new Error("must stop at the renderer bound");
    },
  };
  const result = await loadAllPages(
    "review",
    "unified",
    bridge,
    new QueryCoordinator(bridge),
  );
  assert.equal(result.rows.length, 100_000);
  assert.equal(result.partialError.code, "pagination_limit");
  assert.equal(pageCalls, 0);
});

function read(requestToken, value) {
  return { requestToken, result: Promise.resolve(value) };
}

// The truncated file list banner. A GitLab review whose changes_count is
// "1000+" reaches the shell with changes_overflow set on its detail.

test("a review whose forge capped the file list shows the truncation banner", async () => {
  const detail = trackedDetail({ changes_overflow: true, changes_count: 1000 });
  const view = renderOverflowDiff("overflowed", detail);

  await waitFor(() =>
    assert.deepEqual(warningTexts(view), [CHANGES_OVERFLOW_NOTICE]),
  );
  assert.equal(detail.calls(), 1);
  assert.equal(
    CHANGES_OVERFLOW_NOTICE,
    "The forge returned only part of the changed files, so this list is incomplete. Use Open on forge to see every file.",
  );
  // The banner sits beside the files rather than in place of them.
  assert.equal(view.container.querySelectorAll(".file-path").length > 0, true);
});

test("a complete file list shows no truncation banner", async () => {
  for (const overrides of [{ changes_overflow: false }, {}]) {
    const detail = trackedDetail(overrides);
    const view = renderOverflowDiff("complete", detail);

    await waitFor(() =>
      assert.equal(view.container.querySelectorAll(".file-path").length > 0, true),
    );
    await detail.settled();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(warningTexts(view), []);
    assert.equal(
      view.container.textContent.includes("only part of the changed files"),
      false,
    );
    cleanup();
  }
});

function warningTexts(view) {
  return [...view.container.querySelectorAll(".notice-warning")].map(
    (node) => node.textContent,
  );
}

function trackedDetail(overrides) {
  let calls = 0;
  let last = Promise.resolve();
  return {
    calls: () => calls,
    settled: () => last,
    getReview: (review) => {
      calls += 1;
      const result = Promise.resolve({
        handle: review,
        repository: "repo",
        detail: { ...overflowSummary(), ...overrides },
        capabilities: {
          batched_review: true,
          thread_resolution: true,
          draft_notes: true,
          unapprove: true,
          job_cancel: true,
        },
        revision,
        revision_error: null,
      });
      last = result;
      return { requestToken: crypto.randomUUID(), result };
    },
  };
}

function renderOverflowDiff(review, detail) {
  const bridge = {
    cancelRead: async () => true,
    openExternal: async () => true,
    getReview: detail.getReview,
    openDiff: () => settledRead(overflowDiffPage(review)),
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
    },
    listDiscussions: () =>
      settledRead({
        snapshot_id: "discussions-snapshot",
        resource: review,
        revision: { discussion_count: 0 },
        cursor: 0,
        next_cursor: null,
        discussions: [],
      }),
    pageDiscussions: () => {
      throw new Error("the fixture discussions have one page");
    },
    getReviewMutationCapabilities: () =>
      settledRead({
        review,
        capabilities: {
          general_comment: true,
          inline_comment: true,
          multiline_comment: true,
          reply: true,
          resolve: true,
          approve: true,
          request_changes: true,
          comment_verdict: true,
          atomic_review_batch: false,
        },
      }),
    listReviewDrafts: () =>
      settledRead({ cursor: 0, next_cursor: null, drafts: [] }),
  };
  const feature = createDiffFeature();
  const route = {
    kind: "review",
    item: { handle: review, repository: "repo", summary: overflowSummary() },
    panel: "diff",
  };
  function Harness() {
    const [inlineAnchor, setInlineAnchor] = React.useState(null);
    const queries = React.useMemo(() => new QueryCoordinator(bridge), []);
    return feature.render(
      {
        bridge,
        queries,
        repositories: [
          { handle: "repo", display_name: "acme/widgets", forge_type: "gitlab" },
        ],
        repositoriesReady: true,
        repositoryGeneration: 1,
        reviewPanels: [{ id: "diff", label: "Files changed", order: 20 }],
        inlineAnchor,
        selectInlineAnchor: setInlineAnchor,
        navigate: () => {},
      },
      route,
    );
  }
  return render(React.createElement(Harness));
}

function settledRead(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}

function overflowSummary() {
  return {
    number: 42,
    title: "Vendor a large dependency",
    author: { username: "alice", display_name: "Alice Dev" },
    state: "open",
    is_draft: false,
    source_branch: "feature/vendor",
    target_branch: "main",
    ci_status: "success",
    created_at: "2026-06-01T09:00:00Z",
    updated_at: "2026-06-02T14:30:00Z",
    web_url: "https://gitlab.example.com/acme/widgets/-/merge_requests/42",
    comment_count: 0,
    has_conflicts: false,
    labels: [],
    review_decision: null,
    additions: null,
    deletions: null,
    description: "",
    merge_status: "can_be_merged",
    approvals: [],
    reviewers: [],
    assignees: [],
    changes_count: 3,
    detailed_merge_status: "mergeable",
    draft_notes_count: null,
    status_check_rollup: null,
  };
}

function overflowDiffPage(review) {
  return {
    snapshot_id: "snapshot-overflow",
    resource: review,
    revision,
    cursor: 0,
    next_cursor: null,
    entries: [
      {
        kind: "file",
        file_index: 0,
        old_path: "vendor/file_0.py",
        new_path: "vendor/file_0.py",
        status: "added",
        additions: 1,
        deletions: 0,
        is_binary: false,
        language: "python",
        is_truncated: false,
        is_empty: false,
        is_mode_only: false,
        is_rename_only: false,
        is_unavailable: false,
      },
      {
        kind: "hunk",
        file_index: 0,
        hunk_index: 0,
        header: "@@ -0,0 +1 @@",
        old_start: 0,
        old_count: 0,
        new_start: 1,
        new_count: 1,
        context_text: "",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: null,
        new_line: 1,
        content: "print('hello')",
        line_type: "addition",
      },
    ],
  };
}

test("an old-side discussion on a renamed file resolves to its deleted line", () => {
  const loaded = {
    layout: "unified",
    snapshotId: "snapshot-renamed",
    resource: "renamed",
    revision,
    partialError: null,
    rows: [
      {
        kind: "file",
        file_index: 0,
        old_path: "src/old_calc.py",
        new_path: "src/calc.py",
        status: "renamed",
        additions: 0,
        deletions: 1,
        is_binary: false,
        language: "python",
        is_truncated: false,
        is_empty: false,
        is_mode_only: false,
        is_rename_only: false,
        is_unavailable: false,
      },
      {
        kind: "hunk",
        file_index: 0,
        hunk_index: 0,
        header: "@@ -11 +10,0 @@",
        old_start: 11,
        old_count: 1,
        new_start: 10,
        new_count: 0,
        context_text: "",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: 11,
        new_line: null,
        content: "    return a / b",
        line_type: "deletion",
      },
    ],
  };
  // Both forges store the new path on an old-side comment; the old path is
  // what a comment written against the original file would carry.
  for (const path of ["src/calc.py", "src/old_calc.py"]) {
    const selection = resolveDiscussionTarget("renamed", loaded, {
      discussionId: "d-left",
      path,
      side: "old",
      line: 11,
    });
    assert.notEqual(selection, null, path);
    assert.deepEqual(
      [selection.side, selection.oldLine, selection.oldPath, selection.newPath],
      ["old", 11, "src/old_calc.py", "src/calc.py"],
    );
  }
  assert.equal(
    resolveDiscussionTarget("renamed", loaded, {
      discussionId: "d-elsewhere",
      path: "src/other.py",
      side: "old",
      line: 11,
    }),
    null,
  );
});
