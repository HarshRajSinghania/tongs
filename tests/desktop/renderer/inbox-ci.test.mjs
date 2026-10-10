// The inbox fills each shown row's CI status with its own lookup.
//
// Assertions read strings, counts and booleans only; no DOM node is ever
// passed to assert or returned from a waitFor callback.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  CI_LOADING_TEXT,
  CI_LOOKUP_CONCURRENCY,
  CILookups,
  createInboxFeature,
  inboxFeedPresentation,
  resetInboxListSelections,
} from "../../../desktop/dist/src/renderer/features/inbox/index.js";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "tongs://app/index.html",
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
afterEach(() => {
  cleanup();
  resetInboxListSelections();
});

test("lookups run a few at a time and only for rows without CI", async () => {
  const bridge = ciBridge();
  let changes = 0;
  const lookups = new CILookups(bridge.bridge, () => {
    changes += 1;
  });
  const items = [
    ...Array.from({ length: 9 }, (_, index) => reviewItem(`A1${index}`)),
    reviewItem("B10", "success"),
  ];
  lookups.request(items);
  assert.equal(CI_LOOKUP_CONCURRENCY, 4);
  assert.equal(bridge.open().length, 4);
  assert.equal(lookups.waiting, 9);
  assert.equal(lookups.shown(items[9]), "success");
  assert.equal(lookups.shown(items[0]), null);
  assert.equal(lookups.pending(items[9]), false);

  // Asking again for the same rows queues nothing twice.
  lookups.request(items);
  assert.equal(bridge.calls.length, 4);

  bridge.settle("review-A10", "failed");
  await waitFor(() => assert.equal(bridge.calls.length, 5));
  assert.equal(lookups.shown(items[0]), "failed");
  assert.equal(changes, 1);
  await settleAll(bridge, "success", () => lookups.waiting === 0);
  assert.equal(bridge.calls.length, 9);
  assert.equal(bridge.maxOpen, 4);
  assert.equal(new Set(bridge.calls.map((call) => call.params.review)).size, 9);
  assert.equal(
    bridge.calls.some((call) => call.params.review === "review-B10"),
    false,
  );
  assert.equal(
    bridge.calls.every((call) => !Object.hasOwn(call.params, "fresh")),
    true,
  );
});

test("a failed lookup shows unknown instead of loading forever", async () => {
  const bridge = ciBridge();
  const lookups = new CILookups(bridge.bridge, () => {});
  const item = reviewItem("A12");
  lookups.request([item]);
  bridge.fail("review-A12");
  await waitFor(() => assert.equal(lookups.shown(item), "unknown"));
  assert.equal(lookups.pending(item), false);
  // A row whose lookup finished is not looked up again in this generation.
  lookups.request([item]);
  assert.equal(bridge.calls.length, 1);
});

test("a reset cancels lookups in flight and drops their late results", async () => {
  const bridge = ciBridge();
  const lookups = new CILookups(bridge.bridge, () => {});
  const items = ["A12", "A11"].map((title) => reviewItem(title));
  lookups.request(items);
  const before = bridge.open().map((call) => call.token);
  assert.equal(before.length, 2);

  lookups.reset(true);
  assert.deepEqual(bridge.cancelled, before);
  for (const call of [...bridge.open()]) bridge.settle(call.params.review, "failed");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(lookups.shown(items[0]), null);
  assert.equal(lookups.waiting, 0);

  // The new generation reads past the CI cache after an explicit refresh.
  lookups.request(items);
  assert.deepEqual(
    bridge.open().map((call) => call.params),
    [
      { review: "review-A12", fresh: true },
      { review: "review-A11", fresh: true },
    ],
  );
});

test("CI order uses found statuses and keeps waiting rows with unknown", () => {
  const feeds = [
    {
      repository: "repo-a",
      items: ["A12", "A11", "A10"].map((title) => reviewItem(title)),
      cursor: null,
      loading: false,
      failures: [],
      pages: 1,
    },
    {
      repository: "repo-b",
      items: [reviewItem("B09", "success")],
      cursor: null,
      loading: false,
      failures: [],
      pages: 1,
    },
  ];
  const found = new Map([["review-A10", "failed"]]);
  const ci = {
    shown: (item) =>
      item.summary.ci_status === "unknown"
        ? (found.get(item.handle) ?? null)
        : item.summary.ci_status,
  };
  const ordered = inboxFeedPresentation(feeds, "ci", ci).items.map(
    (item) => item.summary.title,
  );
  assert.deepEqual(ordered, ["A10", "B09", "A11", "A12"]);
});

test("shown rows load with a marker and fill in place without moving", async () => {
  const bridge = ciBridge();
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [repository("repo-a"), repository("repo-b")]),
      { kind: "inbox" },
    ),
  );
  await waitFor(() => assert.equal(bridge.lists.size, 2));
  bridge.respondList(
    "all_open:repo-a",
    page(["A12", "A1150", "A1140", "A1130", "A1120", "A1110"], "cursor-a"),
  );
  bridge.respondList("all_open:repo-b", page(["B1135", "B09"], null));
  // B09 is older than repo-a's frontier, so it is loaded but not shown yet.
  await waitFor(() =>
    assert.deepEqual(cardTitles(view), [
      "A12",
      "A1150",
      "A1140",
      "B1135",
      "A1130",
      "A1120",
      "A1110",
    ]),
  );
  assert.equal(loadingCount(view), 7);
  assert.equal(cardCI(view).every((value) => value.endsWith(`:${CI_LOADING_TEXT}`)), true);
  await waitFor(() => assert.equal(bridge.open().length, CI_LOOKUP_CONCURRENCY));

  bridge.settle("review-A1140", "failed");
  await waitFor(() =>
    assert.equal(cardCI(view).includes("A1140:failed"), true),
  );
  assert.equal(cardTitles(view)[2], "A1140");
  assert.equal(loadingCount(view), 6);

  await settleAll(bridge, "success", () => loadingCount(view) === 0);
  assert.deepEqual(cardCI(view), [
    "A12:success",
    "A1150:success",
    "A1140:failed",
    "B1135:success",
    "A1130:success",
    "A1120:success",
    "A1110:success",
  ]);
  assert.equal(bridge.calls.length, 7);
  assert.equal(bridge.maxOpen, CI_LOOKUP_CONCURRENCY);
  assert.equal(
    bridge.calls.some((call) => call.params.review === "review-B09"),
    false,
  );
});

test("rows the list already knows the CI of are not looked up", async () => {
  const bridge = ciBridge();
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [repository("repo-a")]),
      { kind: "inbox" },
    ),
  );
  await waitFor(() => assert.equal(bridge.lists.size, 1));
  bridge.respondList("all_open:repo-a", {
    items: [reviewItem("A12", "running"), reviewItem("A11")],
    failures: [],
    next_cursor: null,
  });
  await waitFor(() => assert.equal(cardTitles(view).length, 2));
  assert.deepEqual(cardCI(view), ["A12:running", `A11:${CI_LOADING_TEXT}`]);
  assert.deepEqual(
    bridge.calls.map((call) => call.params.review),
    ["review-A11"],
  );
});

test("a tab change cancels lookups and their late results land nowhere", async () => {
  const bridge = ciBridge();
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [repository("repo-a")]),
      { kind: "inbox" },
    ),
  );
  await waitFor(() => assert.equal(bridge.lists.size, 1));
  bridge.respondList("all_open:repo-a", page(["A12", "A11"], null));
  await waitFor(() => assert.equal(bridge.open().length, 2));
  const oldLookups = [...bridge.open()];

  fireEvent.click(view.getByRole("button", { name: "My Reviews" }));
  await waitFor(() => assert.equal(bridge.lists.has("my_reviews:repo-a"), true));
  assert.deepEqual(
    bridge.cancelled.filter((token) => token.startsWith("ci-")),
    oldLookups.map((call) => call.token),
  );

  bridge.respondList("my_reviews:repo-a", page(["A12"], null));
  await waitFor(() => assert.equal(bridge.open().length, 3));
  // The All Open lookup of the same review finishes late with another status.
  for (const call of oldLookups) bridge.resolve(call, "failed");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(cardCI(view), [`A12:${CI_LOADING_TEXT}`]);

  const current = bridge.open().find((call) => !oldLookups.includes(call));
  bridge.resolve(current, "success");
  await waitFor(() => assert.deepEqual(cardCI(view), ["A12:success"]));
});

test("a refresh looks CI up again past the cache and ignores older results", async () => {
  const bridge = ciBridge();
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [repository("repo-a")]),
      { kind: "inbox" },
    ),
  );
  await waitFor(() => assert.equal(bridge.lists.size, 1));
  bridge.respondList("all_open:repo-a", page(["A12", "A11"], null));
  await waitFor(() => assert.equal(bridge.open().length, 2));
  bridge.settle("review-A12", "success");
  await waitFor(() => assert.equal(cardCI(view)[0], "A12:success"));
  const stale = bridge.open()[0];
  assert.equal(stale.params.review, "review-A11");

  fireEvent.click(view.getByRole("button", { name: "Refresh reviews" }));
  await waitFor(() => assert.equal(bridge.lists.size, 1));
  // The previous rows and their statuses stay while the refresh runs.
  assert.deepEqual(cardCI(view), ["A12:success", `A11:${CI_LOADING_TEXT}`]);
  bridge.respondList("all_open:repo-a", page(["A12", "A11"], null));
  await waitFor(() => assert.equal(loadingCount(view), 2));
  assert.equal(bridge.cancelled.includes(stale.token), true);

  bridge.resolve(stale, "failed");
  await waitFor(() => assert.equal(bridge.open().length, 2));
  assert.deepEqual(cardCI(view), [
    `A12:${CI_LOADING_TEXT}`,
    `A11:${CI_LOADING_TEXT}`,
  ]);
  assert.deepEqual(
    bridge.open().map((call) => call.params),
    [
      { review: "review-A12", fresh: true },
      { review: "review-A11", fresh: true },
    ],
  );
  bridge.settle("review-A12", "running");
  bridge.settle("review-A11", "success");
  await waitFor(() =>
    assert.deepEqual(cardCI(view), ["A12:running", "A11:success"]),
  );
});

test("in CI order a row moves once its status arrives and keeps focus", async () => {
  const bridge = ciBridge();
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [repository("repo-a")]),
      { kind: "inbox" },
    ),
  );
  fireEvent.change(view.getByLabelText("Sort reviews"), {
    target: { value: "ci" },
  });
  await waitFor(() => assert.equal(bridge.lists.size, 1));
  bridge.respondList("all_open:repo-a", {
    items: [
      reviewItem("A12", "success"),
      reviewItem("A11"),
      reviewItem("A10"),
    ],
    failures: [],
    next_cursor: null,
  });
  await waitFor(() => assert.equal(bridge.open().length, 2));
  // Waiting rows sort with the unknown ones, by title.
  assert.deepEqual(cardTitles(view), ["A12", "A10", "A11"]);
  view.container.querySelector('[data-review-handle="review-A11"]').focus();
  assert.equal(focusedHandle(), "review-A11");

  bridge.settle("review-A11", "failed");
  await waitFor(() => assert.deepEqual(cardTitles(view), ["A11", "A12", "A10"]));
  assert.equal(focusedHandle(), "review-A11");
  assert.deepEqual(cardCI(view).slice(0, 2), ["A11:failed", "A12:success"]);
});

/** Answer every open lookup, round after round, until `finished` holds. */
async function settleAll(bridge, status, finished) {
  for (let round = 0; round < 100 && !finished(); round += 1) {
    for (const call of [...bridge.open()]) bridge.resolve(call, status);
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.equal(finished(), true);
}

function focusedHandle() {
  return document.activeElement?.getAttribute("data-review-handle") ?? null;
}

function ciBridge() {
  const state = {
    calls: [],
    lists: new Map(),
    cancelled: [],
    maxOpen: 0,
  };
  state.open = () => state.calls.filter((call) => !call.done);
  state.bridge = {
    cancelRead: async (token) => {
      state.cancelled.push(token);
      return true;
    },
    listReviews: (params) => {
      let settle;
      const result = new Promise((resolve, reject) => {
        settle = { resolve, reject };
      });
      state.lists.set(`${params.scope}:${params.repository}`, settle);
      return { requestToken: `list-${state.lists.size}-${params.scope}`, result };
    },
    getReviewCIStatus: (params) => {
      let settle;
      const result = new Promise((resolve, reject) => {
        settle = { resolve, reject };
      });
      const call = { params, token: `ci-${state.calls.length + 1}`, settle, done: false };
      state.calls.push(call);
      state.maxOpen = Math.max(state.maxOpen, state.open().length);
      return { requestToken: call.token, result };
    },
  };
  state.resolve = (call, status) => {
    call.done = true;
    call.settle.resolve({ ci_status: status });
  };
  state.settle = (handle, status) => {
    const call = state.open().find((item) => item.params.review === handle);
    assert.ok(call, `no open lookup for ${handle}`);
    state.resolve(call, status);
  };
  state.fail = (handle) => {
    const call = state.open().find((item) => item.params.review === handle);
    call.done = true;
    call.settle.reject(new Error("lookup failed"));
  };
  state.respondList = (key, value) => {
    const settle = state.lists.get(key);
    state.lists.delete(key);
    settle.resolve(value);
  };
  return state;
}

function loadingCount(view) {
  return view.container.querySelectorAll('[data-ci-loading="true"]').length;
}

function cardTitles(view) {
  return [...view.container.querySelectorAll(".review-card .review-title")].map(
    (node) => node.textContent,
  );
}

function cardCI(view) {
  return [...view.container.querySelectorAll(".review-card")].map(
    (card) =>
      `${card.querySelector(".review-title").textContent}:${card.querySelector(".state").textContent}`,
  );
}

// Titles name their update time: "A1130" is repo A at 11:30, "A12" at 12:00.
function at(title) {
  const digits = title.slice(1);
  const hour = digits.slice(0, 2);
  const minute = digits.length > 2 ? digits.slice(2) : "00";
  return `2026-09-08T${hour}:${minute}:00.000Z`;
}

function reviewItem(title, ciStatus = "unknown") {
  return {
    handle: `review-${title}`,
    repository: `repo-${title[0].toLowerCase()}`,
    summary: {
      number: 1,
      title,
      author: { username: "author", display_name: "Author" },
      state: "open",
      is_draft: false,
      source_branch: "feature",
      target_branch: "main",
      ci_status: ciStatus,
      created_at: "2026-09-01T00:00:00Z",
      updated_at: at(title),
      web_url: "https://example.invalid/review/1",
      comment_count: 0,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 1,
      deletions: 0,
    },
  };
}

function page(itemTitles, nextCursor) {
  return {
    items: itemTitles.map((title) => reviewItem(title)),
    failures: [],
    next_cursor: nextCursor,
  };
}

function repository(handle) {
  return { handle, display_name: handle, forge_type: "github" };
}

function featureContext(bridge, repositories) {
  return {
    bridge,
    queries: new QueryCoordinator(bridge),
    repositories,
    repositoriesReady: true,
    repositoryGeneration: 1,
    reviewPanels: [],
    inlineAnchor: null,
    selectInlineAnchor: () => {},
    navigate: () => {},
  };
}
