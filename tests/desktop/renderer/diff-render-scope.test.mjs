// #306: a click or a drag in a large diff must not redo work for every changed
// file. The changed-file list renders one memoised entry per file, and this
// file counts how often each entry renders.
//
// The count comes from the React DevTools global hook, the integration point
// React DOM reports every commit to, so the product code carries no test
// probe. The hook is installed before React DOM loads, which is why the
// Testing Library and the renderer modules are loaded after it. An entry
// rendered in a commit holds a props object no earlier commit held: a memoised
// component that bails out keeps its previous props object.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

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

const renders = new Map();
const seenProps = new WeakSet();
globalThis.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
  supportsFiber: true,
  renderers: new Map(),
  inject(renderer) {
    const id = this.renderers.size + 1;
    this.renderers.set(id, renderer);
    return id;
  },
  checkDCE() {},
  onScheduleFiberRoot() {},
  onCommitFiberRoot(_rendererId, root) {
    countFileNavigationRenders(root.current);
  },
  onCommitFiberUnmount() {},
  onPostCommitFiberRoot() {},
};

function countFileNavigationRenders(rootFiber) {
  const stack = [rootFiber];
  while (stack.length > 0) {
    const fiber = stack.pop();
    if (fiber.sibling) stack.push(fiber.sibling);
    if (fiber.child) stack.push(fiber.child);
    if (typeof fiber.type !== "function" || fiber.type.name !== "FileNavigation")
      continue;
    const props = fiber.memoizedProps;
    if (seenProps.has(props)) continue;
    seenProps.add(props);
    const index = props.file.file_index;
    renders.set(index, (renders.get(index) ?? 0) + 1);
  }
}

function renderCounts() {
  return [...renders.entries()].sort((left, right) => left[0] - right[0]);
}

const { cleanup, fireEvent, render, waitFor } = desktopRequire(
  "@testing-library/react",
);
const React = desktopRequire("react");
const { QueryCoordinator } = await import(
  "../../../desktop/dist/src/renderer/core/query.js"
);
const { createDiffFeature } = await import(
  "../../../desktop/dist/src/renderer/features/diff/index.js"
);
const { clearPendingEdit } = await import(
  "../../../desktop/dist/src/renderer/features/review/drawer.js"
);

afterEach(() => {
  cleanup();
  clearPendingEdit();
  renders.clear();
});

const revision = { head_sha: "head", base_sha: "base", start_sha: null };
const REVIEW = "review-306";
const FILES = 6;
const LINES = 4;

test("selecting or dragging lines re-renders no changed-file entry", async () => {
  const view = renderDiff();
  await waitFor(() => assert.equal(filePaths(view).length, FILES));
  await waitFor(() => assert.equal(lineAnchorCount(view), LINES));
  await settle();
  // The probe saw every entry mount, so a zero below is a real zero.
  assert.deepEqual(
    renderCounts().map(([index, count]) => [index, count > 0]),
    Array.from({ length: FILES }, (_, index) => [index, true]),
  );

  renders.clear();
  fireEvent.click(lineAnchor(view, 2));
  await waitFor(() => assert.equal(selectedLineCount(view), 1));
  await settle();
  assert.deepEqual(renderCounts(), []);

  renders.clear();
  fireEvent.mouseDown(lineAnchor(view, 1));
  for (let line = 2; line <= LINES; line += 1) {
    fireEvent.mouseOver(lineAnchor(view, line), { buttons: 1 });
  }
  fireEvent.mouseUp(document);
  await waitFor(() => assert.equal(selectedLineCount(view), LINES));
  await settle();
  assert.deepEqual(renderCounts(), []);
});

test("moving to another file re-renders only the two entries whose state changed", async () => {
  const view = renderDiff();
  await waitFor(() => assert.equal(filePaths(view).length, FILES));
  await waitFor(() => assert.equal(lineAnchorCount(view), LINES));
  await settle();

  renders.clear();
  fireEvent.click(fileButton(view, 3));
  await waitFor(() => assert.equal(activeFilePath(view), path(3)));
  await waitFor(() => assert.equal(lineAnchorCount(view), LINES));
  await settle();
  assert.deepEqual(renderCounts(), [
    [0, 1],
    [3, 1],
  ]);

  // A hunk in the file already open moves the reader without a file change.
  renders.clear();
  fireEvent.click(hunkButtons(view)[3]);
  await settle();
  assert.equal(activeFilePath(view), path(3));
  assert.deepEqual(renderCounts(), []);
});

async function settle() {
  for (let turn = 0; turn < 5; turn += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

function path(index) {
  return `src/file_${index}.py`;
}

function diffEntries() {
  const entries = [];
  for (let index = 0; index < FILES; index += 1) {
    entries.push({
      kind: "file",
      file_index: index,
      old_path: path(index),
      new_path: path(index),
      status: "modified",
      additions: LINES,
      deletions: 0,
      is_binary: false,
      language: "python",
      is_truncated: false,
      is_empty: false,
      is_mode_only: false,
      is_rename_only: false,
      is_unavailable: false,
    });
    entries.push({
      kind: "hunk",
      file_index: index,
      hunk_index: 0,
      header: `@@ -0,0 +1,${LINES} @@`,
      old_start: 0,
      old_count: 0,
      new_start: 1,
      new_count: LINES,
      context_text: "",
    });
    for (let line = 1; line <= LINES; line += 1) {
      entries.push({
        kind: "line",
        file_index: index,
        hunk_index: 0,
        old_line: null,
        new_line: line,
        content: `value_${index}_${line} = ${line}`,
        line_type: "addition",
      });
    }
  }
  return entries;
}

function summary() {
  return {
    number: 306,
    title: "A large review",
    author: { username: "alice", display_name: "Alice Dev" },
    state: "open",
    is_draft: false,
    source_branch: "feature/large",
    target_branch: "main",
    ci_status: "success",
    created_at: "2026-06-01T09:00:00Z",
    updated_at: "2026-06-02T14:30:00Z",
    web_url: "https://gitlab.example.com/acme/widgets/-/merge_requests/306",
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
    changes_count: FILES,
    detailed_merge_status: "mergeable",
    draft_notes_count: null,
    status_check_rollup: null,
  };
}

function settledRead(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}

function diffBridge() {
  return {
    cancelRead: async () => true,
    openExternal: async () => true,
    getReview: (review) =>
      settledRead({
        handle: review,
        repository: "repo",
        detail: summary(),
        capabilities: {
          batched_review: true,
          thread_resolution: true,
          draft_notes: true,
          unapprove: true,
          job_cancel: true,
        },
        revision,
        revision_error: null,
      }),
    openDiff: (params) =>
      settledRead({
        snapshot_id: "snapshot-306",
        resource: REVIEW,
        revision,
        cursor: 0,
        next_cursor: null,
        entries: params.layout === "unified" ? diffEntries() : [],
      }),
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
    },
    listDiscussions: () =>
      settledRead({
        snapshot_id: "discussions-snapshot",
        resource: REVIEW,
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
        review: REVIEW,
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

function renderDiff() {
  const bridge = diffBridge();
  const feature = createDiffFeature();
  const route = {
    kind: "review",
    item: { handle: REVIEW, repository: "repo", summary: summary() },
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

function fileButtons(view) {
  return [
    ...view.container.querySelectorAll(".file-list .file-item:not(.hunk-item)"),
  ];
}

function fileButton(view, index) {
  const button = fileButtons(view)[index];
  if (!button) throw new Error(`no file button ${index}`);
  return button;
}

function hunkButtons(view) {
  return [...view.container.querySelectorAll(".file-list .hunk-item")];
}

function filePaths(view) {
  return fileButtons(view).map(
    (button) => button.querySelector("span")?.textContent ?? "",
  );
}

function activeFilePath(view) {
  return (
    view.container.querySelector(".file-list .nav-item-active span")
      ?.textContent ?? null
  );
}

function lineAnchorCount(view) {
  return view.container.querySelectorAll(".line-anchor").length;
}

function selectedLineCount(view) {
  return view.container.querySelectorAll(".line-selected").length;
}

function lineAnchor(view, line) {
  const anchor = view.container.querySelector(
    `.line-anchor[aria-label="Select new line ${line}"]`,
  );
  if (!anchor) throw new Error(`no new line ${line} anchor`);
  return anchor;
}
