import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { InlineAnchorStore } from "../../../desktop/dist/src/renderer/core/navigation.js";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createDiffFeature } from "../../../desktop/dist/src/renderer/features/diff/index.js";
import {
  MAX_VIEWED_REVIEWS,
  clearViewedFiles,
  setFileViewed,
  viewedFileKey,
  viewedFilesFor,
  viewedProgressText,
} from "../../../desktop/dist/src/renderer/features/diff/viewed.js";
import { clearPendingEdit } from "../../../desktop/dist/src/renderer/features/review/drawer.js";

// #195: each file header in the desktop diff carries a Viewed checkbox that
// collapses the file, and the review header says how many files are viewed.
// The marks last for the app session and reset on a new head commit.

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
const { cleanup, fireEvent, render, waitFor } = desktopRequire("@testing-library/react");
const React = desktopRequire("react");
afterEach(() => {
  cleanup();
  clearPendingEdit();
  clearViewedFiles();
});

function revisionAt(head) {
  return { head_sha: head, base_sha: "base", start_sha: null };
}

function fileRow(index, path) {
  return {
    kind: "file",
    file_index: index,
    old_path: path,
    new_path: path,
    status: "modified",
    additions: 1,
    deletions: 0,
    is_binary: false,
    language: "python",
    is_truncated: false,
    is_empty: false,
    is_mode_only: false,
    is_rename_only: false,
    is_unavailable: false,
  };
}

function fileRows(fileIndex, layout) {
  const line = {
    old_line: null,
    new_line: 1,
    content: `value_${fileIndex} = ${fileIndex}`,
    line_type: "addition",
  };
  return [
    fileRow(fileIndex, `src/file_${fileIndex}.py`),
    {
      kind: "hunk",
      file_index: fileIndex,
      hunk_index: 0,
      header: "@@ -0,0 +1 @@",
      old_start: 0,
      old_count: 0,
      new_start: 1,
      new_count: 1,
      context_text: `hunk_${fileIndex}`,
    },
    layout === "split"
      ? {
          kind: "split",
          file_index: fileIndex,
          hunk_index: 0,
          row_index: 0,
          old: null,
          new: { ...line, anchor_side: "new" },
        }
      : { kind: "line", file_index: fileIndex, hunk_index: 0, ...line },
  ];
}

function settledRead(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}

function summary() {
  return {
    number: 195,
    title: "Mark files as viewed",
    author: { username: "alice", display_name: "Alice Dev" },
    state: "open",
    is_draft: false,
    source_branch: "feature/viewed",
    target_branch: "main",
    ci_status: "success",
    created_at: "2026-06-01T09:00:00Z",
    updated_at: "2026-06-02T14:30:00Z",
    web_url: "https://gitlab.example.com/acme/widgets/-/merge_requests/195",
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
    changes_count: 2,
    detailed_merge_status: "mergeable",
    draft_notes_count: null,
    status_check_rollup: null,
  };
}

/**
 * A bridge whose diff has two files. `heads` names the head SHA each forge
 * read returns in turn; a layout switch re-projects the last one.
 */
function viewedBridge(review, heads = ["head-1"]) {
  const opens = [];
  let reads = 0;
  let head = heads[0];
  return {
    opens,
    cancelRead: async () => true,
    openExternal: async () => true,
    getReview: (handle) =>
      settledRead({
        handle,
        repository: "repo",
        detail: summary(),
        capabilities: {
          batched_review: true,
          thread_resolution: true,
          draft_notes: true,
          unapprove: true,
          job_cancel: true,
        },
        revision: revisionAt(head),
        revision_error: null,
      }),
    openDiff: (params) => {
      opens.push({ ...params });
      if (params.from_snapshot === undefined) {
        head = heads[Math.min(reads, heads.length - 1)];
        reads += 1;
      }
      return settledRead({
        snapshot_id: `snapshot-${opens.length}`,
        resource: review,
        revision: revisionAt(head),
        cursor: 0,
        next_cursor: null,
        entries: [...fileRows(0, params.layout), ...fileRows(1, params.layout)],
      });
    },
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
}

function renderDiff(review, bridge, extra = {}) {
  const feature = createDiffFeature();
  const route = {
    kind: "review",
    item: { handle: review, repository: "repo", summary: summary() },
    panel: "diff",
    ...extra,
  };
  function Harness() {
    const inlineAnchors = React.useMemo(() => new InlineAnchorStore(), []);
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
        inlineAnchors,
        navigate: () => {},
      },
      route,
    );
  }
  return render(React.createElement(Harness));
}

function texts(view, selector) {
  return [...view.container.querySelectorAll(selector)].map(
    (node) => node.textContent,
  );
}

function progress(view) {
  return texts(view, ".review-header .review-progress");
}

function lines(view) {
  return texts(view, ".diff-view .line-content");
}

/** The shown file's Viewed checkbox. Only the shown file has a header. */
function viewedBox(view) {
  const boxes = view.container.querySelectorAll(".diff-file .file-viewed input");
  assert.equal(boxes.length, 1);
  return boxes[0];
}

function navEntries(view) {
  return texts(view, ".file-list .file-item:not(.hunk-item)");
}

async function loaded(view, expected = ["+value_0 = 0"]) {
  await waitFor(() => assert.deepEqual(lines(view), expected));
}

test("toggling Viewed collapses the file to its header and expands it again", async () => {
  const review = "review-viewed-toggle";
  const view = renderDiff(review, viewedBridge(review));
  await loaded(view);
  assert.deepEqual(texts(view, ".diff-file .file-viewed"), ["Viewed"]);
  assert.equal(viewedBox(view).checked, false);
  assert.deepEqual(texts(view, ".file-list .hunk-item"), ["hunk_0", "hunk_1"]);

  fireEvent.click(viewedBox(view));

  assert.equal(viewedBox(view).checked, true);
  assert.deepEqual(lines(view), []);
  assert.deepEqual(texts(view, ".diff-view .diff-hunk"), []);
  // The header stays, so the file can be cleared again.
  assert.deepEqual(texts(view, ".diff-view .file-path"), ["src/file_0.py"]);
  assert.deepEqual(texts(view, ".diff-viewed-note"), [
    "This file is marked as viewed. Clear Viewed to show its changes.",
  ]);
  assert.deepEqual(navEntries(view), [
    "src/file_0.pyViewed +1 −0",
    "src/file_1.py+1 −0",
  ]);
  assert.deepEqual(texts(view, ".file-list .hunk-item"), ["hunk_1"]);

  fireEvent.click(viewedBox(view));

  assert.equal(viewedBox(view).checked, false);
  assert.deepEqual(lines(view), ["+value_0 = 0"]);
  assert.deepEqual(texts(view, ".diff-viewed-note"), []);
  assert.deepEqual(navEntries(view), ["src/file_0.py+1 −0", "src/file_1.py+1 −0"]);
  assert.deepEqual(texts(view, ".file-list .hunk-item"), ["hunk_0", "hunk_1"]);
});

test("the review header counts the viewed files as they change", async () => {
  const review = "review-viewed-progress";
  const view = renderDiff(review, viewedBridge(review));
  await loaded(view);
  assert.deepEqual(progress(view), ["0 of 2 files viewed"]);
  assert.equal(
    view.container.querySelector(".review-progress").getAttribute("role"),
    "status",
  );

  fireEvent.click(viewedBox(view));
  assert.deepEqual(progress(view), ["1 of 2 files viewed"]);

  fireEvent.click(view.getByText("src/file_1.py"));
  await loaded(view, ["+value_1 = 1"]);
  assert.equal(viewedBox(view).checked, false);
  fireEvent.click(viewedBox(view));
  assert.deepEqual(progress(view), ["2 of 2 files viewed"]);

  fireEvent.click(viewedBox(view));
  assert.deepEqual(progress(view), ["1 of 2 files viewed"]);
  // Going back to the first file finds it still collapsed.
  fireEvent.click(view.getByText("src/file_0.py"));
  assert.equal(viewedBox(view).checked, true);
  assert.deepEqual(lines(view), []);
});

test("the marks survive a layout switch and a remount at the same revision", async () => {
  const review = "review-viewed-session";
  const bridge = viewedBridge(review);
  const view = renderDiff(review, bridge);
  await loaded(view);
  fireEvent.click(viewedBox(view));
  assert.deepEqual(progress(view), ["1 of 2 files viewed"]);

  fireEvent.click(view.getByText("Split"));
  await waitFor(() => assert.equal(bridge.opens.length, 2));
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".diff-split").length, 1),
  );
  // Split draws the header in both panes; only the new side has the checkbox.
  assert.equal(
    view.container.querySelectorAll(".diff-split .diff-file").length,
    2,
  );
  assert.equal(viewedBox(view).checked, true);
  assert.equal(
    view.container.querySelectorAll(".diff-split .split-cell").length,
    0,
  );
  assert.deepEqual(progress(view), ["1 of 2 files viewed"]);

  // Leaving the review and coming back keeps the marks for this app session.
  cleanup();
  const again = renderDiff(review, viewedBridge(review));
  await waitFor(() => assert.deepEqual(progress(again), ["1 of 2 files viewed"]));
  assert.equal(viewedBox(again).checked, true);
  assert.deepEqual(lines(again), []);
});

test("a new revision resets the viewed files and their count", async () => {
  const review = "review-viewed-revision";
  const bridge = viewedBridge(review, ["head-1", "head-1", "head-2"]);
  const view = renderDiff(review, bridge);
  await loaded(view);
  fireEvent.click(viewedBox(view));
  assert.deepEqual(progress(view), ["1 of 2 files viewed"]);

  // A Refresh that brings the same head commit keeps the marks.
  fireEvent.click(view.getByText("Refresh"));
  await waitFor(() => assert.equal(bridge.opens.length, 2));
  await waitFor(() =>
    assert.equal(
      view.container.querySelector(".revision")?.getAttribute("data-head-sha"),
      "head-1",
    ),
  );
  await waitFor(() => assert.equal(texts(view, ".notice-loading").length, 0));
  assert.deepEqual(progress(view), ["1 of 2 files viewed"]);
  assert.equal(viewedBox(view).checked, true);

  // New commits pushed: the next Refresh reads another head SHA.
  fireEvent.click(view.getByText("Refresh"));
  await waitFor(() =>
    assert.equal(
      view.container.querySelector(".revision")?.getAttribute("data-head-sha"),
      "head-2",
    ),
  );
  assert.deepEqual(progress(view), ["0 of 2 files viewed"]);
  assert.equal(viewedBox(view).checked, false);
  assert.deepEqual(lines(view), ["+value_0 = 0"]);
  assert.deepEqual(navEntries(view), ["src/file_0.py+1 −0", "src/file_1.py+1 −0"]);
});

test("the Viewed checkbox is labelled, focusable and toggled by Space", async () => {
  const review = "review-viewed-keyboard";
  const view = renderDiff(review, viewedBridge(review));
  await loaded(view);
  const box = viewedBox(view);
  assert.equal(box.getAttribute("type"), "checkbox");
  assert.equal(box.closest("label")?.textContent, "Viewed");
  assert.equal(box.tabIndex, 0);
  assert.equal(box.disabled, false);
  // The label names it, so the accessible query finds the same control.
  assert.equal(view.getByLabelText("Viewed").getAttribute("type"), "checkbox");

  box.focus();
  assert.equal(document.activeElement?.getAttribute("type"), "checkbox");

  // The press is claimed, so the browser's click on release cannot toggle it
  // back.
  const notPrevented = fireEvent.keyDown(box, { key: " ", code: "Space" });
  assert.equal(notPrevented, false);
  assert.equal(viewedBox(view).checked, true);
  assert.deepEqual(lines(view), []);
  assert.deepEqual(progress(view), ["1 of 2 files viewed"]);
  // The focus stays on the checkbox, so a second Space clears it.
  assert.equal(document.activeElement?.getAttribute("type"), "checkbox");

  fireEvent.keyDown(viewedBox(view), { key: " ", code: "Space" });
  assert.equal(viewedBox(view).checked, false);
  assert.deepEqual(lines(view), ["+value_0 = 0"]);
  assert.deepEqual(progress(view), ["0 of 2 files viewed"]);

  // A held Space repeats the key; only the first press toggles.
  assert.equal(
    fireEvent.keyDown(viewedBox(view), { key: " ", code: "Space", repeat: true }),
    false,
  );
  assert.equal(viewedBox(view).checked, false);

  // Other keys and modified Space are left alone.
  for (const init of [
    { key: "Enter", code: "Enter" },
    { key: " ", code: "Space", ctrlKey: true },
  ]) {
    assert.equal(fireEvent.keyDown(viewedBox(view), init), true);
    assert.equal(viewedBox(view).checked, false);
  }
});

test("a discussion jump into a viewed file shows the line and clears the mark once", async () => {
  const review = "review-viewed-jump";
  setFileViewed(review, "head-1", viewedFileKey(fileRow(0, "src/file_0.py")), true);
  setFileViewed(review, "head-1", viewedFileKey(fileRow(1, "src/file_1.py")), true);
  const jump = {
    discussionId: "discussion-1",
    path: "src/file_1.py",
    side: "new",
    line: 1,
  };
  const view = renderDiff(review, viewedBridge(review), { diffTarget: jump });
  await loaded(view, ["+value_1 = 1"]);
  assert.equal(viewedBox(view).checked, false);
  assert.deepEqual(progress(view), ["1 of 2 files viewed"]);
  assert.deepEqual(navEntries(view), [
    "src/file_0.pyViewed +1 −0",
    "src/file_1.py+1 −0",
  ]);

  // The reader marks it again after reading. Coming back to the tab keeps the
  // route's jump, which is not a new request to read the line.
  fireEvent.click(viewedBox(view));
  assert.deepEqual(progress(view), ["2 of 2 files viewed"]);
  cleanup();
  const again = renderDiff(review, viewedBridge(review), { diffTarget: jump });
  await waitFor(() =>
    assert.deepEqual(texts(again, ".diff-view .file-path"), ["src/file_1.py"]),
  );
  assert.equal(viewedBox(again).checked, true);
  assert.deepEqual(lines(again), []);
  assert.deepEqual(progress(again), ["2 of 2 files viewed"]);
});

test("the viewed store keeps one revision per review and a bounded number of reviews", () => {
  const file = fileRow(0, "src/a.py");
  const other = fileRow(1, "src/b.py");
  const key = viewedFileKey(file);
  setFileViewed("store-review", "head-1", key, true);
  assert.equal(viewedFilesFor("store-review", "head-1").has(key), true);
  assert.equal(viewedFilesFor("store-review", "head-2").size, 0);
  assert.equal(viewedFilesFor("another-review", "head-1").size, 0);
  assert.equal(viewedProgressText([file, other], viewedFilesFor("store-review", "head-1")), "1 of 2 files viewed");
  assert.equal(viewedProgressText([file], new Set()), "0 of 1 file viewed");

  // A mark at a new head replaces the old revision's marks.
  setFileViewed("store-review", "head-2", viewedFileKey(other), true);
  assert.equal(viewedFilesFor("store-review", "head-1").size, 0);
  assert.deepEqual(
    [...viewedFilesFor("store-review", "head-2")],
    [viewedFileKey(other)],
  );

  // A rename is a different file from its old path.
  const renamed = { ...file, new_path: "src/renamed.py" };
  assert.notEqual(viewedFileKey(renamed), key);

  for (let index = 0; index < MAX_VIEWED_REVIEWS; index += 1)
    setFileViewed(`bulk-${index}`, "head", key, true);
  // The oldest review is forgotten first.
  assert.equal(viewedFilesFor("store-review", "head-2").size, 0);
  assert.equal(viewedFilesFor("bulk-0", "head").size, 1);
  assert.equal(viewedFilesFor(`bulk-${MAX_VIEWED_REVIEWS - 1}`, "head").size, 1);
});
