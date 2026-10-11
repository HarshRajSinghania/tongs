import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { after } from "node:test";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM(
  "<!doctype html><html><head></head><body><div id='app'></div></body></html>",
  { url: "tongs://app/index.html" },
);
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
  DOMException: dom.window.DOMException,
  requestAnimationFrame: (callback) => setTimeout(callback, 0),
});
dom.window.requestAnimationFrame = globalThis.requestAnimationFrame;
dom.window.HTMLCanvasElement.prototype.getContext = () => null;
const React = desktopRequire("react");
const { fireEvent, queryByLabelText, render, waitFor } = desktopRequire(
  "@testing-library/react",
);

after(() => dom.window.close());

const STOPPED = "Local service not running. Quit and reopen Tongs.";
const UPDATES = "Updates are available. Refresh the current view.";
const DISCOVERY_FAILURE = "The configured scan root could not be read";

// The app mounts itself when its module loads, so every test in this file
// shares one mounted app over one bridge fixture.
const fixture = bridgeFixture();
let mounted = null;
function mountApp() {
  if (mounted === null) {
    Object.defineProperty(window, "tongs", { value: fixture.bridge });
    mounted = import("../../../desktop/dist/src/renderer/app.js");
  }
  return mounted;
}

test("mounted app reconciles successful discovery without losing review work", async () => {
  // The first discovery fails, so the main pane says why and offers a retry
  // instead of waiting for discovery forever.
  fixture.discoveries.unshift(discoveryFailure());
  await mountApp();
  await waitFor(() => assert.equal(mainRetryCount(), 1));
  assert.equal(mainText().includes(DISCOVERY_FAILURE), true);
  assert.equal(
    mainText().includes("Waiting for local repository discovery"),
    false,
  );
  assert.equal(mainRetryDisabled(), false);
  fireEvent.click(mainRetry());
  await waitFor(() => assert.equal(hasButton("Repo A"), true));
  assert.equal(mainRetryCount(), 0);
  assert.equal(mainText().includes(DISCOVERY_FAILURE), false);

  await waitFor(() => assert.ok(button("Repo A")));
  fireEvent.click(button("Repo A"));
  await waitFor(() => assert.equal(title(), "Repo A"));

  fixture.discoveries.push([repositoryA(), repositoryB()]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() =>
    assert.equal(button("Refresh local repositories").disabled, false),
  );
  assert.equal(title(), "Repo A");
  assert.ok(routeNotice() === null);

  fixture.discoveries.push(new Error("controlled discovery failure"));
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() => assert.ok(button("Retry")));
  assert.equal(title(), "Repo A");
  assert.ok(routeNotice() === null);

  fixture.discoveries.push([repositoryB(), repositoryA("Repo A renamed")]);
  fireEvent.click(button("Retry"));
  await waitFor(() => assert.equal(title(), "Repo A renamed"));
  assert.ok(routeNotice() === null);

  fireEvent.click(await awaitReview("Persistent review"));
  // Overview holds the general composer, and its primary is what starts a
  // review now: the Discussions panel writes nothing at all.
  await waitFor(() => assert.ok(labelled("General review comment")));
  setValue(labelled("General review comment"), "started from Overview");
  await waitFor(() => assert.equal(button("Start a review").disabled, false));
  fireEvent.click(button("Start a review"));
  await waitFor(() => assert.ok(button("Add to review")));
  setValue(labelled("General review comment"), "unsent general composer");

  // The review's Summary is the drawer's, reached from the Discussions tab.
  fireEvent.click(button("Discussions"));
  await waitFor(() => assert.equal(hasDrawerToggle(), true));
  fireEvent.click(drawerToggle());
  await waitFor(() => assert.equal(labelledValue("Summary"), ""));
  setValue(labelled("Summary"), "unsaved local draft body");
  fireEvent.click(drawerToggle());

  // The anchored buffer is the in-diff composer's now. It holds whatever was
  // typed on one line, a suggestion block included, because Insert suggestion
  // pre-fills this same per-anchor body rather than a buffer of its own.
  fireEvent.click(button("Files changed"));
  fireEvent.click(await awaitGutter("Comment on new line 1"));
  await waitFor(() => assert.ok(labelled("Inline review comment")));
  setValue(labelled("Inline review comment"), "unsent anchored composer");

  const writesBeforeRemoval = fixture.writes.length;
  const readsBeforeRemoval = fixture.reviewReads.length;
  fixture.discoveries.push([repositoryB()]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() => assert.equal(title(), "All reviews"));
  assert.match(
    routeNotice()?.textContent ?? "",
    /no longer in the local workspace/,
  );
  await waitFor(() =>
    assert.equal(
      fixture.reviewReads
        .slice(readsBeforeRemoval)
        .some((params) => params.repository === "repo-a"),
      false,
    ),
  );
  assert.equal(fixture.writes.length, writesBeforeRemoval);
  assert.equal(Boolean(labelled("General review comment")), false);
  assert.equal(Boolean(labelled("Inline review comment")), false);
  assert.equal(
    document.body.textContent.includes("unsent general composer"),
    false,
  );
  assert.equal(
    document.body.textContent.includes("unsent anchored composer"),
    false,
  );
  assert.equal(
    document.body.textContent.includes("unsaved local draft body"),
    false,
  );

  fixture.discoveries.push([repositoryA("Repo A restored"), repositoryB()]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() => assert.ok(routeNotice() === null));
  fireEvent.click(button("Repo A restored"));
  fireEvent.click(await awaitReview("Persistent review"));
  await waitFor(() =>
    assert.equal(
      labelledValue("General review comment"),
      "unsent general composer",
    ),
  );
  fireEvent.click(await awaitButton("Discussions"));
  await waitFor(() => assert.equal(hasDrawerToggle(), true));
  fireEvent.click(drawerToggle());
  await waitFor(() =>
    assert.equal(labelledValue("Summary"), "unsaved local draft body"),
  );
  fireEvent.click(drawerToggle());
  assert.ok(document.body.textContent.includes("Draft review active"));

  // The anchored text returns to its own line and to no other.
  fireEvent.click(button("Files changed"));
  fireEvent.click(await awaitGutter("Comment on new line 2"));
  await waitFor(() => assert.equal(labelledValue("Inline review comment"), ""));
  fireEvent.click(button("Cancel"));
  fireEvent.click(await awaitGutter("Comment on new line 1"));
  await waitFor(() =>
    assert.equal(
      labelledValue("Inline review comment"),
      "unsent anchored composer",
    ),
  );
  assert.equal(fixture.writes.length, writesBeforeRemoval);

  fireEvent.click(button("← Reviews"));
  fireEvent.click(await awaitButton("Repo A restored"));
  await waitFor(() => assert.equal(title(), "Repo A restored"));
  fixture.discoveries.push([]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() => assert.equal(title(), "All reviews"));
  assert.match(routeNotice()?.textContent ?? "", /Showing All reviews/);
  assert.equal(fixture.writes.length, writesBeforeRemoval);

  fireEvent.click(await awaitButton("Plugin dashboard"));
  await waitFor(() => assert.equal(title(), "Plugin dashboard"));
  fixture.discoveries.push([repositoryB(), repositoryA("Repo A current")]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() =>
    assert.equal(button("Refresh local repositories").disabled, false),
  );
  assert.equal(title(), "Plugin dashboard");
  assert.ok(routeNotice() === null);

  // The header follows the main-process service status, not only the probe.
  await waitFor(() => assert.equal(serviceStatus(), "Local service connected"));
  fixture.publishServiceStatus({ state: "stopped", revision: 1 });
  await waitFor(() => assert.equal(serviceStatus(), STOPPED));
  fixture.publishServiceStatus({ state: "connected", revision: 2 });
  await waitFor(() => assert.equal(serviceStatus(), "Local service connected"));
});

test("the header warns about updates only on a resync and a route change clears it", async () => {
  await mountApp();
  await waitFor(() => assert.equal(serviceStatus(), "Local service connected"));

  // Review and pipeline changes refresh the views that own them.
  fixture.emit("service.changed", { kind: "review_changed", resource: "review-a" });
  fixture.emit("service.changed", { kind: "pipeline_changed", resource: null });
  fixture.emit("service.changed", { kind: "repositories_changed" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(serviceStatus(), "Local service connected");
  assert.equal(serviceStatusClass(), "service-status service-status-ready");

  fixture.emit("protocol.resync_required", { reason: "event_overflow" });
  await waitFor(() => assert.equal(serviceStatus(), UPDATES));
  assert.equal(serviceStatusClass(), "service-status service-status-warning");
  fireEvent.click(await awaitButton("All reviews"));
  await waitFor(() => assert.equal(serviceStatus(), "Local service connected"));

  fixture.emit("service.changed", { kind: "resync_required" });
  await waitFor(() => assert.equal(serviceStatus(), UPDATES));
  fireEvent.click(await awaitButton("Repo B"));
  await waitFor(() => assert.equal(serviceStatus(), "Local service connected"));
  assert.equal(title(), "Repo B");
});

test("the app bar's colour scheme toggle marks the document root", async () => {
  await mountApp();
  await waitFor(() =>
    assert.equal(document.documentElement.dataset.colorScheme, "system"),
  );
  const choices = () =>
    [...document.querySelectorAll(".app-bar [data-color-scheme-choice]")].map(
      (item) => `${item.textContent}:${item.getAttribute("aria-pressed")}`,
    );
  assert.deepEqual(choices(), ["System:true", "Light:false", "Dark:false"]);
  fireEvent.click(document.querySelector('[data-color-scheme-choice="light"]'));
  await waitFor(() =>
    assert.equal(document.documentElement.dataset.colorScheme, "light"),
  );
  assert.deepEqual(choices(), ["System:false", "Light:true", "Dark:false"]);
  assert.deepEqual(fixture.colorSchemes, ["light"]);
  fireEvent.click(document.querySelector('[data-color-scheme-choice="system"]'));
  await waitFor(() =>
    assert.equal(document.documentElement.dataset.colorScheme, "system"),
  );
  assert.deepEqual(fixture.colorSchemes, ["light", "system"]);
});

test("the view error fallback offers Try again, which mounts the view afresh", async () => {
  const { ErrorBoundary } = await mountApp();
  let failing = true;
  let mounts = 0;
  function FlakyView() {
    if (failing) throw new Error("raw failure text with token ghp_secret");
    React.useEffect(() => {
      mounts += 1;
    }, []);
    return React.createElement("p", { className: "flaky-view" }, "Recovered view");
  }
  const logged = [];
  const original = console.error;
  console.error = (...args) => logged.push(args.map((arg) => String(arg)));
  const host = document.createElement("div");
  document.body.append(host);
  let view;
  try {
    view = render(
      React.createElement(ErrorBoundary, null, React.createElement(FlakyView)),
      { container: host },
    );
    await waitFor(() =>
      assert.equal(host.querySelectorAll("[role='alert']").length, 1),
    );
  } finally {
    console.error = original;
  }
  assert.equal(
    host.querySelector(".view-title")?.textContent ?? null,
    "This view could not be displayed",
  );
  const tryAgain = () =>
    [...host.querySelectorAll("button")].filter(
      (item) => item.textContent.trim() === "Try again",
    );
  assert.equal(tryAgain().length, 1);
  assert.equal(tryAgain()[0].disabled, false);
  // The boundary's own log line is the safe text alone.
  const own = logged.filter(
    (args) =>
      args.length === 1 &&
      args[0] === "The local service could not complete this read.",
  );
  assert.equal(own.length, 1);
  assert.equal(mounts, 0);

  failing = false;
  fireEvent.click(tryAgain()[0]);
  await waitFor(() =>
    assert.equal(host.querySelector(".flaky-view")?.textContent ?? null, "Recovered view"),
  );
  assert.equal(mounts, 1);
  assert.equal(tryAgain().length, 0);
  assert.equal(host.querySelectorAll("[role='alert']").length, 0);
  view.unmount();
  host.remove();
});

function discoveryFailure() {
  return Object.assign(new Error(DISCOVERY_FAILURE), {
    code: "scan_failed",
    retryable: true,
  });
}

function mainText() {
  return document.querySelector("main#content")?.textContent ?? "";
}

function mainRetryButtons() {
  return [...document.querySelectorAll("main#content button")].filter(
    (item) => item.textContent.trim() === "Retry",
  );
}

function mainRetryCount() {
  return mainRetryButtons().length;
}

function mainRetryDisabled() {
  return mainRetryButtons()[0]?.disabled ?? null;
}

function mainRetry() {
  return mainRetryButtons()[0];
}

function hasButton(name) {
  return button(name) !== undefined;
}

function serviceStatusClass() {
  return document.querySelector("#service-status")?.className ?? null;
}

function serviceStatus() {
  return document.querySelector("#service-status")?.textContent ?? null;
}

function bridgeFixture() {
  let sequence = 0;
  let activeDraft = null;
  const discoveries = [[repositoryA(), repositoryB()]];
  const reviewReads = [];
  const writes = [];
  const serviceListeners = new Set();
  const eventListeners = new Set();
  const appearanceListeners = new Set();
  let appearance = { configured: "system", saved: null, effective: "system", revision: 0 };
  const colorSchemes = [];
  let eventSequence = 0;
  const read = (value) => ({
    requestToken: `request-${++sequence}`,
    result:
      value instanceof Error ? Promise.reject(value) : Promise.resolve(value),
  });
  const bridge = {
    discoverRepositories: () => {
      const next = discoveries.shift() ?? [];
      return read(next instanceof Error ? next : { repositories: next });
    },
    listReviews: (params) => {
      reviewReads.push(params);
      return read({
        items: params.repository === "repo-a" ? [reviewItem()] : [],
        failures: [],
      });
    },
    getReview: () => read(reviewSnapshot()),
    listDiscussions: () => read(discussionsPage([])),
    getReviewMutationCapabilities: () =>
      read({ review: "review-a", capabilities: mutationCapabilities() }),
    getReviewActionCapabilities: () =>
      read({
        review: "review-a",
        capabilities: { merge: false, close: false, reopen: false, unapprove: false },
      }),
    listReviewDrafts: () =>
      read({
        cursor: 0,
        next_cursor: null,
        drafts: activeDraft ? [activeDraft] : [],
      }),
    listReviewSubmissions: () =>
      read({ cursor: 0, next_cursor: null, attempts: [] }),
    createReviewDraft: async () => {
      writes.push("create-draft");
      activeDraft = draft();
      return activeDraft;
    },
    saveReviewDraft: async (params) => {
      writes.push("save-draft");
      activeDraft = {
        ...draft(),
        version: params.expected_version + 1,
        body: params.content.body,
        verdict: params.content.verdict,
        comments: params.content.comments,
      };
      return activeDraft;
    },
    openDiff: ({ layout }) => read(diffPage(layout)),
    pageDiff: () => {
      throw new Error("No next page");
    },
    listPlugins: () => read({ plugins: [plugin()] }),
    listAssets: () => read([]),
    setLocation: async () => ({ accepted: true }),
    cancelRead: async () => true,
    onEvent: (listener) => {
      eventListeners.add(listener);
      return () => eventListeners.delete(listener);
    },
    getServiceStatus: async () => ({ state: "connected", revision: 0 }),
    onServiceStatus: (listener) => {
      serviceListeners.add(listener);
      return () => serviceListeners.delete(listener);
    },
    openExternal: async () => true,
    getAppearance: async () => appearance,
    setColorScheme: async (scheme) => {
      colorSchemes.push(scheme);
      const saved = scheme === "system" ? null : scheme;
      appearance = {
        ...appearance,
        saved,
        effective: saved ?? appearance.configured,
        revision: appearance.revision + 1,
      };
      for (const listener of appearanceListeners) listener(appearance);
      return appearance;
    },
    onAppearance: (listener) => {
      appearanceListeners.add(listener);
      return () => appearanceListeners.delete(listener);
    },
  };
  const publishServiceStatus = (status) => {
    for (const listener of serviceListeners) listener(status);
  };
  const emit = (name, data) => {
    const event = { sequence: ++eventSequence, name, data };
    for (const listener of [...eventListeners]) listener(event);
  };
  return {
    bridge,
    discoveries,
    reviewReads,
    writes,
    colorSchemes,
    publishServiceStatus,
    emit,
  };
}

function repositoryA(displayName = "Repo A") {
  return { handle: "repo-a", display_name: displayName, forge_type: "github" };
}

function repositoryB() {
  return { handle: "repo-b", display_name: "Repo B", forge_type: "gitlab" };
}

function plugin() {
  return {
    plugin_id: "fixture-plugin",
    state: "started",
    has_terminal_entry_point: true,
    has_desktop_entry_point: true,
    error: null,
    manifest: {
      title: "Fixture plugin",
      version: "1.0.0",
      api_major: 1,
      modules: [
        {
          id: "dashboard",
          title: "Plugin dashboard",
          entry_asset: "fixture-module",
          stylesheets: ["fixture-style"],
        },
      ],
      navigation: [
        {
          id: "dashboard",
          title: "Plugin dashboard",
          module_id: "dashboard",
        },
      ],
      commands: [],
      methods: [],
      events: [],
      focus_targets: [],
      help_asset: null,
      reads: [],
      assets_available: true,
    },
  };
}

function reviewItem() {
  return {
    handle: "review-a",
    repository: "repo-a",
    summary: {
      number: 95,
      title: "Persistent review",
      author: { username: "author", display_name: "Author" },
      state: "open",
      is_draft: false,
      source_branch: "feature",
      target_branch: "main",
      ci_status: "success",
      created_at: "2026-09-08T00:00:00Z",
      updated_at: "2026-09-08T00:00:00Z",
      web_url: "https://example.invalid/review/95",
      comment_count: 0,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 1,
      deletions: 0,
    },
  };
}

function reviewSnapshot() {
  return {
    handle: "review-a",
    repository: "repo-a",
    detail: { ...reviewItem().summary, description: "", merge_status: "can_be_merged" },
    capabilities: {
      batched_review: true,
      thread_resolution: true,
      draft_notes: true,
      unapprove: false,
      job_cancel: false,
    },
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    revision_error: null,
  };
}

function mutationCapabilities() {
  return {
    general_comment: true,
    inline_comment: true,
    multiline_comment: true,
    reply: true,
    resolve: true,
    approve: true,
    request_changes: true,
    comment_verdict: true,
    atomic_review_batch: false,
  };
}

function draft() {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    review: "review-a",
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    version: 1,
    body: "",
    verdict: null,
    comments: [],
    state: "editable",
    created_at: "2026-09-08T00:00:00+00:00",
    updated_at: "2026-09-08T00:00:00+00:00",
  };
}

function diffPage(layout) {
  return {
    snapshot_id: `snapshot-${layout}`,
    resource: "review-a",
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    cursor: 0,
    next_cursor: null,
    entries: [
      {
        kind: "file",
        file_index: 0,
        old_path: "src/example.py",
        new_path: "src/example.py",
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
      },
      {
        kind: "hunk",
        file_index: 0,
        hunk_index: 0,
        header: "@@ -1,2 +1,2 @@",
        old_start: 1,
        old_count: 2,
        new_start: 1,
        new_count: 2,
        context_text: "",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: 1,
        new_line: 1,
        content: "selected line",
        line_type: "context",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: 2,
        new_line: 2,
        content: "alternate selected line",
        line_type: "context",
      },
    ],
  };
}

function button(name) {
  return [...document.querySelectorAll("button")].find(
    (item) => item.textContent.trim() === name,
  );
}

/** The in-diff gutter affordance, which names itself with an aria-label. */
async function awaitGutter(name) {
  return waitFor(() => {
    const found = document.querySelector(`button[aria-label="${name}"]`);
    assert.equal(Boolean(found), true);
    return found;
  });
}

async function awaitButton(name) {
  await waitFor(() => assert.ok(button(name)));
  return button(name);
}

async function awaitReview(name) {
  await waitFor(() => assert.ok(reviewButton(name)));
  return reviewButton(name);
}

function reviewButton(name) {
  return [...document.querySelectorAll(".review-card")].find((item) =>
    item.querySelector(".review-title")?.textContent.trim() === name,
  );
}

/** The "Your review" button, which carries a count and so has no fixed text. */
function drawerToggle() {
  return document.querySelector(".review-drawer-toggle") ?? undefined;
}

/**
 * Booleans and strings rather than elements, so a failed wait reports what it
 * looked for instead of serialising the mounted tree into the failure.
 */
function hasDrawerToggle() {
  return document.querySelectorAll(".review-drawer-toggle").length === 1;
}

function labelledValue(name) {
  return labelled(name)?.value ?? null;
}

function labelled(name) {
  return queryByLabelText(document.body, name) ?? undefined;
}

function setValue(element, value) {
  fireEvent.change(element, { target: { value } });
}

function title() {
  return document.querySelector(".view-title")?.textContent ?? null;
}

function routeNotice() {
  return [...document.querySelectorAll("main > .notice")].find((item) =>
    item.textContent.includes("selected repository"),
  ) ?? null;
}

/** A first discussions page that holds every thread, as the sidecar sends it. */
function discussionsPage(discussions) {
  return { discussions, revision: { discussion_count: discussions.length } };
}
