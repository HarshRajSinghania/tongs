// #384: the desktop diff colours code by language in both layouts. Token
// colours come from highlight.js class names, the old and new sides of each
// hunk are highlighted apart, and a file in an unknown language or past the
// size cutoff stays plain text. Every assertion reads strings, class names and
// counts, never a DOM node.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
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
const {
  MAX_HIGHLIGHT_CHARACTERS,
  MAX_HIGHLIGHT_LINES,
  grammarFor,
  highlightFile,
  highlightLines,
} = await import("../../../desktop/dist/src/renderer/features/diff/syntax.js");
const { clearPendingEdit } = await import(
  "../../../desktop/dist/src/renderer/features/review/drawer.js"
);

afterEach(() => {
  cleanup();
  clearPendingEdit();
});

const revision = { head_sha: "head", base_sha: "base", start_sha: null };

/** One token per entry: its classes outermost first, then its text. */
function tokenStrings(lines) {
  return lines.map((line) =>
    line.map((token) => `${token.scopes.join(">") || "plain"}:${token.text}`),
  );
}

test("a Python file is split into highlight.js tokens line by line", () => {
  const lines = highlightLines("python", [
    "def greet(name):",
    '    """Say hi.',
    '    """',
    "    return 1",
  ]);
  assert.deepEqual(tokenStrings(lines), [
    [
      "hljs-keyword:def",
      "plain: ",
      "hljs-title function_:greet",
      "plain:(",
      "hljs-params:name",
      "plain:):",
    ],
    // The docstring spans two lines, and both are coloured as a string.
    ["plain:    ", 'hljs-string:"""Say hi.'],
    ['hljs-string:    """'],
    ["plain:    ", "hljs-keyword:return", "plain: ", "hljs-number:1"],
  ]);
});

test("a TypeScript file is split into highlight.js tokens line by line", () => {
  const lines = highlightLines("typescript", [
    "const total: number = 1;",
    "// done",
  ]);
  assert.deepEqual(tokenStrings(lines), [
    [
      "hljs-keyword:const",
      "plain: ",
      "hljs-attr:total",
      "plain:: ",
      "hljs-built_in:number",
      "plain: = ",
      "hljs-number:1",
      "plain:;",
    ],
    ["hljs-comment:// done"],
  ]);
});

test("Pygments aliases map to the bundled grammars and anything else stays plain", () => {
  assert.deepEqual(
    [
      "python",
      "tsx",
      "jsx",
      "toml",
      "tsql",
      "docker",
      "html",
      "shell",
      "cobol",
      "text",
      null,
    ].map(grammarFor),
    [
      "python",
      "typescript",
      "javascript",
      "ini",
      "sql",
      "dockerfile",
      "xml",
      "bash",
      null,
      null,
      null,
    ],
  );
  assert.equal(highlightLines("cobol", ["DISPLAY 'HI'."]), null);
  assert.equal(highlightLines(null, ["x = 1"]), null);
});

test("each side of a hunk is highlighted on its own in unified view", async () => {
  const view = renderDiff(bridgeFor(sidesEntries("python")));
  await waitFor(() =>
    assert.equal(lineTexts(view, ".diff-unified .line-content").length, 4),
  );
  // Highlighting adds spans only: the line text is unchanged.
  assert.deepEqual(lineTexts(view, ".diff-unified .line-content"), [
    " def f():",
    '\u2212    x = """',
    "+    x = 1",
    "     return x",
  ]);
  assert.deepEqual(lineTokens(view, ".diff-unified .line-content"), [
    ["hljs-keyword:def", "hljs-title function_:f"],
    ['hljs-string:"""'],
    ["hljs-number:1"],
    // The removed line opened a string on the old side only, so the context
    // line after it still reads as code.
    ["hljs-keyword:return"],
  ]);
});

test("each side of a hunk is highlighted on its own in split view", async () => {
  const view = renderDiff(bridgeFor(sidesEntries("python")));
  await waitFor(() =>
    assert.equal(lineTexts(view, ".diff-unified .line-content").length, 4),
  );
  fireEvent.click(view.getByText("Split"));
  const oldSide = ".split-pane-old .split-cell .line-content";
  const newSide = ".split-pane-new .split-cell .line-content";
  await waitFor(() => assert.equal(lineTexts(view, newSide).length, 3));
  assert.deepEqual(lineTexts(view, oldSide), [
    " def f():",
    '\u2212    x = """',
    "     return x",
  ]);
  assert.deepEqual(lineTokens(view, newSide), [
    ["hljs-keyword:def", "hljs-title function_:f"],
    ["hljs-number:1"],
    ["hljs-keyword:return"],
  ]);
  // The old file really has an open string there, and only its pane says so.
  assert.deepEqual(lineTokens(view, oldSide), [
    ["hljs-keyword:def", "hljs-title function_:f"],
    ['hljs-string:"""'],
    ["hljs-string:    return x"],
  ]);
});

test("a file in an unknown language is drawn as plain text", async () => {
  for (const language of ["cobol", null]) {
    const view = renderDiff(bridgeFor(sidesEntries(language)));
    await waitFor(() =>
      assert.equal(lineTexts(view, ".diff-unified .line-content").length, 4),
    );
    assert.deepEqual(lineTexts(view, ".diff-unified .line-content"), [
      " def f():",
      '\u2212    x = """',
      "+    x = 1",
      "     return x",
    ]);
    assert.equal(
      view.container.querySelectorAll(".line-content span").length,
      0,
    );
    cleanup();
  }
});

test("a file past the line or size cutoff is not highlighted", () => {
  const file = fileRow(0, "python");
  const atLimit = addedRows(MAX_HIGHLIGHT_LINES, () => "x = 1");
  assert.equal(highlightFile(file, [file, ...atLimit]) !== null, true);

  const longer = fileRow(0, "python");
  const overLimit = addedRows(MAX_HIGHLIGHT_LINES + 1, () => "x = 1");
  assert.equal(highlightFile(longer, [longer, ...overLimit]), null);

  const wide = fileRow(0, "python");
  const full = addedRows(1, () => "x".repeat(MAX_HIGHLIGHT_CHARACTERS));
  assert.equal(highlightFile(wide, [wide, ...full]) !== null, true);

  const wider = fileRow(0, "python");
  const overflow = addedRows(1, () => "x".repeat(MAX_HIGHLIGHT_CHARACTERS + 1));
  assert.equal(highlightFile(wider, [wider, ...overflow]), null);
});

test("a split context line counts once toward the line cutoff", () => {
  const file = fileRow(0, "python");
  const rows = [file];
  for (let index = 0; index < MAX_HIGHLIGHT_LINES; index += 1) {
    const cell = (anchor) => ({
      old_line: index + 1,
      new_line: index + 1,
      content: "x = 1",
      line_type: "context",
      anchor_side: anchor,
    });
    rows.push({
      kind: "split",
      file_index: 0,
      hunk_index: 0,
      row_index: index,
      old: cell("old"),
      new: cell("new"),
    });
  }
  assert.equal(highlightFile(file, rows) !== null, true);
});

test("a file is highlighted once until its rows change", () => {
  const file = fileRow(0, "python");
  const rows = [file, ...addedRows(3, (line) => `value_${line} = ${line}`)];
  const first = highlightFile(file, rows);
  assert.equal(first !== null, true);
  // The same rows in a new array, as a re-render or a layout pass builds them.
  assert.equal(highlightFile(file, [...rows]) === first, true);
  const grown = [...rows, ...addedRows(1, () => "extra = 4")];
  assert.equal(highlightFile(file, grown) === first, false);
});

const stylesPath = path.resolve(
  import.meta.dirname,
  "../../../desktop/dist/shell/styles.css",
);
const css = readFileSync(stylesPath, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

test("syntax token rules set the foreground colour only", () => {
  const rules = [...css.matchAll(/([^{}]*)\{([^{}]*)\}/g)]
    .filter((match) => match[1].includes(".hljs-"))
    .map((match) => ({
      selectors: match[1].trim(),
      properties: [...match[2].matchAll(/([a-z-]+)\s*:/g)].map((m) => m[1]),
    }));
  assert.equal(rules.length > 0, true);
  for (const rule of rules) {
    assert.deepEqual(rule.properties, ["color"], rule.selectors);
  }
});

test("every syntax colour token has a light scheme value", () => {
  const lightStart = css.search(/@media\s*\(\s*prefers-color-scheme\s*:\s*light\s*\)/);
  const base = css.slice(0, lightStart);
  const light = css.slice(lightStart);
  const names = (text) =>
    [...new Set([...text.matchAll(/(--syntax-[a-z-]+)\s*:/g)].map((m) => m[1]))].sort();
  assert.equal(names(base).length > 0, true);
  assert.deepEqual(names(light), names(base));
});

function sidesEntries(language) {
  const file = fileRow(0, language);
  const hunk = {
    kind: "hunk",
    file_index: 0,
    hunk_index: 0,
    header: "@@ -1,3 +1,3 @@",
    old_start: 1,
    old_count: 3,
    new_start: 1,
    new_count: 3,
    context_text: "",
  };
  const line = (old_line, new_line, content, line_type) => ({
    kind: "line",
    file_index: 0,
    hunk_index: 0,
    old_line,
    new_line,
    content,
    line_type,
  });
  const cell = (old_line, new_line, content, line_type, anchor_side) => ({
    old_line,
    new_line,
    content,
    line_type,
    anchor_side,
  });
  const split = (row_index, old, fresh) => ({
    kind: "split",
    file_index: 0,
    hunk_index: 0,
    row_index,
    old,
    new: fresh,
  });
  return {
    unified: [
      file,
      hunk,
      line(1, 1, "def f():", "context"),
      line(2, null, '    x = """', "deletion"),
      line(null, 2, "    x = 1", "addition"),
      line(3, 3, "    return x", "context"),
    ],
    split: [
      file,
      hunk,
      split(
        0,
        cell(1, 1, "def f():", "context", "old"),
        cell(1, 1, "def f():", "context", "new"),
      ),
      split(
        1,
        cell(2, null, '    x = """', "deletion", "old"),
        cell(null, 2, "    x = 1", "addition", "new"),
      ),
      split(
        2,
        cell(3, 3, "    return x", "context", "old"),
        cell(3, 3, "    return x", "context", "new"),
      ),
    ],
  };
}

function fileRow(index, language) {
  return {
    kind: "file",
    file_index: index,
    old_path: `src/file_${index}`,
    new_path: `src/file_${index}`,
    status: "modified",
    additions: 1,
    deletions: 1,
    is_binary: false,
    language,
    is_truncated: false,
    is_empty: false,
    is_mode_only: false,
    is_rename_only: false,
    is_unavailable: false,
    old_mode: null,
    new_mode: null,
  };
}

function addedRows(count, content) {
  return Array.from({ length: count }, (_, index) => ({
    kind: "line",
    file_index: 0,
    hunk_index: 0,
    old_line: null,
    new_line: index + 1,
    content: content(index + 1),
    line_type: "addition",
  }));
}

function settledRead(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}

function summary() {
  return {
    number: 384,
    title: "Syntax colours",
    author: { username: "alice", display_name: "Alice Dev" },
    state: "open",
    is_draft: false,
    source_branch: "feature/colours",
    target_branch: "main",
    ci_status: "success",
    created_at: "2026-06-01T09:00:00Z",
    updated_at: "2026-06-02T14:30:00Z",
    web_url: "https://gitlab.example.com/acme/widgets/-/merge_requests/384",
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
    changes_count: 1,
    detailed_merge_status: "mergeable",
    draft_notes_count: null,
    status_check_rollup: null,
  };
}

const REVIEW = "review-384";

function bridgeFor(entries) {
  let snapshots = 0;
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
    openDiff: (params) => {
      snapshots += 1;
      return settledRead({
        snapshot_id: `snapshot-${snapshots}`,
        resource: REVIEW,
        revision,
        cursor: 0,
        next_cursor: null,
        entries: entries[params.layout],
      });
    },
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

function renderDiff(bridge) {
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

function lineTexts(view, selector) {
  return [...view.container.querySelectorAll(selector)].map(
    (node) => node.textContent,
  );
}

/** Each line's highlight.js spans as "class:text" strings, in order. */
function lineTokens(view, selector) {
  return [...view.container.querySelectorAll(selector)].map((line) =>
    [...line.querySelectorAll('span[class^="hljs-"]')].map(
      (span) => `${span.className}:${span.textContent}`,
    ),
  );
}
