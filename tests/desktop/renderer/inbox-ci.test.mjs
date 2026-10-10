// The inbox fills in the CI status of each row near the viewport with its
// own lookup.
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
  CI_NEAR_MARGIN,
  CI_RETRY_DELAY_MS,
  CI_SORT_SETTLE_MS,
  CI_UNAVAILABLE,
  CI_UNAVAILABLE_TEXT,
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

test("lookups start up to the bound and only for rows without CI", async () => {
  const bridge = ciBridge();
  let changes = 0;
  const lookups = new CILookups(
    bridge.bridge,
    () => {
      changes += 1;
    },
    { concurrency: 4 },
  );
  const items = [
    ...Array.from({ length: 9 }, (_, index) => reviewItem(`A1${index}`)),
    reviewItem("B10", "success"),
  ];
  lookups.request(items);
  // A screen of GitHub lookups is in flight at once, so it batches.
  assert.equal(CI_LOOKUP_CONCURRENCY, 16);
  assert.equal(bridge.open().length, 4);
  assert.equal(lookups.waiting, 9);
  assert.equal(lookups.shown(items[9]), "success");
  assert.equal(lookups.shown(items[0]), null);
  assert.equal(lookups.pending(items[9]), false);

  // Naming the same rows again queues nothing twice.
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

test("rows that leave the viewport before their lookup starts are dropped", async () => {
  const bridge = ciBridge();
  const lookups = new CILookups(bridge.bridge, () => {}, { concurrency: 2 });
  const items = ["A", "B", "C", "D", "E", "F", "G"].map((name) =>
    reviewItem(`${name}12`),
  );
  lookups.request(items.slice(0, 4));
  assert.deepEqual(reviews(bridge.open()), ["review-A12", "review-B12"]);
  assert.equal(lookups.waiting, 4);

  // Scrolled on: C and D never started, so they leave the queue.
  lookups.request(items.slice(4, 6));
  assert.equal(lookups.waiting, 4);
  await settleAll(bridge, "success", () => lookups.waiting === 0);
  assert.deepEqual(reviews(bridge.calls), [
    "review-A12",
    "review-B12",
    "review-E12",
    "review-F12",
  ]);
  // Rows never named near the viewport are never looked up.
  assert.equal(lookups.shown(items[2]), null);
  assert.equal(lookups.shown(items[6]), null);
});

test("cancelled lookups hold their slot until they settle", async () => {
  const bridge = ciBridge();
  const lookups = new CILookups(bridge.bridge, () => {}, { concurrency: 2 });
  const [a, b, c, d] = ["A12", "B12", "C12", "D12"].map((title) =>
    reviewItem(title),
  );
  lookups.request([a, b]);
  const stale = [...bridge.open()];
  lookups.reset();
  assert.deepEqual(
    bridge.cancelled,
    stale.map((call) => call.token),
  );
  lookups.request([c, d]);
  // Both cancelled reads are still pending in the main process.
  assert.equal(bridge.calls.length, 2);
  assert.equal(lookups.waiting, 2);

  bridge.resolve(stale[0], "failed");
  await waitFor(() => assert.equal(bridge.calls.length, 3));
  assert.equal(bridge.calls[2].params.review, "review-C12");
  assert.equal(lookups.shown(a), null);
  bridge.resolve(stale[1], "failed");
  await waitFor(() => assert.equal(bridge.calls.length, 4));
  assert.equal(bridge.calls[3].params.review, "review-D12");
});

test("a failed lookup is unavailable, retried once later and when shown again", async () => {
  const bridge = ciBridge();
  const clock = fakeTimers();
  const lookups = new CILookups(bridge.bridge, () => {}, {
    now: clock.now,
    timer: clock.timer,
  });
  const item = reviewItem("A12");
  lookups.request([item]);
  bridge.fail("review-A12");
  await waitFor(() => assert.equal(lookups.shown(item), CI_UNAVAILABLE));
  assert.notEqual(lookups.shown(item), "unknown");
  assert.equal(lookups.rateLimited(item), false);
  assert.equal(lookups.pending(item), true);
  // Naming the row again while it stays in view does not retry at once.
  lookups.request([item]);
  assert.equal(bridge.calls.length, 1);

  clock.advance(CI_RETRY_DELAY_MS);
  assert.equal(bridge.calls.length, 2);
  bridge.fail("review-A12");
  await waitFor(() => assert.equal(lookups.waiting, 0));
  assert.equal(clock.scheduled, 0);
  assert.equal(lookups.shown(item), CI_UNAVAILABLE);
  // The timed retry happens once only.
  clock.advance(CI_RETRY_DELAY_MS * 10);
  assert.equal(bridge.calls.length, 2);

  // Scrolled away and back, the row is tried again.
  lookups.request([]);
  lookups.request([item]);
  assert.equal(bridge.calls.length, 3);
  bridge.settle("review-A12", "success");
  await waitFor(() => assert.equal(lookups.shown(item), "success"));
});

test("a rate limit pauses the list's lookups until it resets", async () => {
  const bridge = ciBridge();
  const clock = fakeTimers();
  const lookups = new CILookups(bridge.bridge, () => {}, {
    concurrency: 2,
    now: clock.now,
    timer: clock.timer,
  });
  const [a, b, c] = ["A12", "B12", "C12"].map((title) => reviewItem(title));
  lookups.request([a, b, c]);
  assert.deepEqual(reviews(bridge.open()), ["review-A12", "review-B12"]);

  bridge.rateLimit("review-A12", 60);
  await waitFor(() => assert.equal(lookups.pausedUntil, clock.now() + 60_000));
  assert.equal(lookups.shown(a), CI_UNAVAILABLE);
  assert.equal(lookups.rateLimited(a), true);
  // Rows still waiting show unavailable too, and their lookups hold.
  assert.equal(lookups.shown(c), CI_UNAVAILABLE);
  assert.equal(lookups.rateLimited(c), true);
  assert.equal(bridge.calls.length, 2);
  bridge.settle("review-B12", "success");
  await waitFor(() => assert.equal(lookups.shown(b), "success"));
  lookups.request([a, b, c]);
  assert.equal(bridge.calls.length, 2);
  assert.equal(lookups.waiting, 0);

  clock.advance(59_000);
  assert.equal(bridge.calls.length, 2);
  clock.advance(1_000);
  assert.equal(lookups.pausedUntil, null);
  assert.deepEqual(reviews(bridge.open()), ["review-A12", "review-C12"]);
  bridge.settle("review-A12", "failed");
  bridge.settle("review-C12", "running");
  await waitFor(() => assert.equal(lookups.waiting, 0));
  assert.deepEqual([a, b, c].map((item) => lookups.shown(item)), [
    "failed",
    "success",
    "running",
  ]);
});

test("only rows marked by a refresh read past the CI cache", async () => {
  const bridge = ciBridge();
  const lookups = new CILookups(bridge.bridge, () => {});
  const items = ["A12", "A11", "A10"].map((title) => reviewItem(title));
  lookups.request(items);
  bridge.settle("review-A12", "success");
  await waitFor(() => assert.equal(lookups.shown(items[0]), "success"));

  lookups.reset();
  assert.equal(lookups.shown(items[0]), null);
  lookups.markFresh(["review-A12", "review-A11"]);
  lookups.request(items);
  assert.deepEqual(
    bridge.open().slice(-3).map((call) => call.params),
    [
      { review: "review-A12", fresh: true },
      { review: "review-A11", fresh: true },
      { review: "review-A10" },
    ],
  );
});

test("CI order uses found statuses and keeps waiting rows with unknown", () => {
  const feeds = [
    {
      repository: "repo-a",
      items: ["A12", "A11", "A10", "A09"].map((title) => reviewItem(title)),
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
  const found = new Map([
    ["review-A10", "failed"],
    ["review-A09", CI_UNAVAILABLE],
  ]);
  const ci = {
    shown: (item) =>
      item.summary.ci_status === "unknown"
        ? (found.get(item.handle) ?? null)
        : item.summary.ci_status,
  };
  const ordered = inboxFeedPresentation(feeds, "ci", ci).items.map(
    (item) => item.summary.title,
  );
  // An unavailable row sorts with the unknown ones, by title.
  assert.deepEqual(ordered, ["A10", "B09", "A09", "A11", "A12"]);
});

test("only rows near the viewport are looked up, on every tab", async () => {
  const observers = installObservers();
  try {
    const bridge = ciBridge();
    const titles = Array.from(
      { length: 40 },
      (_, index) => `A11${String(59 - index).padStart(2, "0")}`,
    );
    observers.show(titles.slice(0, 5).map((title) => `review-${title}`));
    const view = render(
      createInboxFeature().render(
        featureContext(bridge.bridge, [repository("repo-a")]),
        { kind: "inbox" },
      ),
    );
    await waitFor(() => assert.equal(bridge.lists.size, 1));
    bridge.respondList("all_open:repo-a", page(titles, null));
    await waitFor(() => assert.equal(bridge.calls.length, 5));
    assert.deepEqual(
      reviews(bridge.calls),
      titles.slice(0, 5).map((title) => `review-${title}`),
    );
    await settleAll(bridge, "success", () => bridge.open().length === 0);
    assert.equal(
      observers.rowOptions().every((options) => options.rootMargin === CI_NEAR_MARGIN),
      true,
    );
    assert.equal(observers.rowOptions().length, 1);
    // Rows not looked up yet keep the loading marker.
    await waitFor(() => assert.equal(loadingCount(view), 35));

    // A long scroll: 16 rows start, and the four still queued are dropped
    // once the view moves on before their lookups start.
    observers.show(titles.slice(5, 25).map((title) => `review-${title}`));
    await waitFor(() => assert.equal(bridge.open().length, CI_LOOKUP_CONCURRENCY));
    observers.show(titles.slice(30, 33).map((title) => `review-${title}`));
    await settleAll(bridge, "success", () => bridge.open().length === 0);
    const looked = new Set(reviews(bridge.calls));
    assert.equal(bridge.calls.length, 5 + CI_LOOKUP_CONCURRENCY + 3);
    for (const title of titles.slice(21, 25))
      assert.equal(looked.has(`review-${title}`), false, title);
    for (const title of [...titles.slice(25, 30), ...titles.slice(33)])
      assert.equal(looked.has(`review-${title}`), false, title);
    assert.equal(loadingCount(view), 40 - looked.size);

    // Another tab follows the same rule.
    observers.show(["review-A1159"]);
    fireEvent.click(view.getByRole("button", { name: "My MRs" }));
    await waitFor(() => assert.equal(bridge.lists.has("my_mrs:repo-a"), true));
    const before = bridge.calls.length;
    bridge.respondList("my_mrs:repo-a", page(titles.slice(0, 10), null));
    await waitFor(() => assert.equal(bridge.calls.length, before + 1));
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.deepEqual(reviews(bridge.calls.slice(before)), ["review-A1159"]);
  } finally {
    observers.uninstall();
  }
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
  await waitFor(() => assert.equal(bridge.open().length, 7));

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
  assert.equal(
    bridge.calls.some((call) => call.params.review === "review-B09"),
    false,
  );
});

test("a failed lookup shows CI as unavailable, not unknown", async () => {
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
  bridge.fail("review-A11");
  bridge.settle("review-A12", "unknown");
  await waitFor(() =>
    assert.deepEqual(cardCI(view), ["A12:unknown", `A11:${CI_UNAVAILABLE_TEXT}`]),
  );
  assert.deepEqual(unavailable(view), [
    "A11:failed:CI status unavailable",
  ]);
  assert.equal(loadingCount(view), 0);
  assert.equal(view.queryAllByRole("alert").length, 0);
});

test("a rate limit shows a notice and holds the list's lookups", async () => {
  const bridge = ciBridge();
  const titles = Array.from(
    { length: 20 },
    (_, index) => `A11${String(59 - index).padStart(2, "0")}`,
  );
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [repository("repo-a")]),
      { kind: "inbox" },
    ),
  );
  await waitFor(() => assert.equal(bridge.lists.size, 1));
  bridge.respondList("all_open:repo-a", page(titles, null));
  await waitFor(() => assert.equal(bridge.open().length, CI_LOOKUP_CONCURRENCY));

  bridge.rateLimit("review-A1159", 600);
  await waitFor(() =>
    assert.match(
      view.getByRole("alert").textContent,
      /^Forge rate limit reached\. CI status lookups resume at /,
    ),
  );
  // The rows still queued wait for the reset instead of starting.
  await settleAll(bridge, "success", () => bridge.open().length === 0);
  assert.equal(bridge.calls.length, CI_LOOKUP_CONCURRENCY);
  const held = unavailable(view);
  assert.equal(held.length, 20 - (CI_LOOKUP_CONCURRENCY - 1));
  assert.equal(
    held.every((value) =>
      value.endsWith(
        ":rate_limited:CI status unavailable until the forge rate limit resets",
      ),
    ),
    true,
  );
  assert.equal(loadingCount(view), 0);
});

test("a refresh reads CI past the cache only for the rows it shows", async () => {
  const bridge = ciBridge();
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [repository("repo-a")]),
      { kind: "inbox" },
    ),
  );
  await waitFor(() => assert.equal(bridge.lists.size, 1));
  bridge.respondList("all_open:repo-a", page(["A12", "A11"], "cursor-a"));
  await waitFor(() => assert.equal(bridge.open().length, 2));
  bridge.settle("review-A12", "success");
  await waitFor(() => assert.equal(cardCI(view)[0], "A12:success"));
  const stale = bridge.open()[0];
  assert.equal(stale.params.review, "review-A11");

  fireEvent.click(view.getByRole("button", { name: "Refresh reviews" }));
  await waitFor(() => assert.equal(bridge.lists.size, 1));
  // The previous rows and their statuses stay while the refresh runs.
  assert.deepEqual(cardCI(view), ["A12:success", `A11:${CI_LOADING_TEXT}`]);
  bridge.respondList("all_open:repo-a", page(["A12", "A11"], "cursor-a"));
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

  // Rows that load more adds after the refresh use the CI cache.
  fireEvent.click(view.getByRole("button", { name: "Load more" }));
  await waitFor(() => assert.equal(bridge.lists.size, 1));
  bridge.respondList("all_open:repo-a", page(["A10"], null));
  await waitFor(() => assert.equal(bridge.open().length, 1));
  assert.deepEqual(bridge.open()[0].params, { review: "review-A10" });
});

test("in CI order rows re-sort once the shown lookups settle and keep focus", async () => {
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

  // A result arrives while another shown row still waits: the row fills in
  // place and the order holds.
  bridge.settle("review-A11", "failed");
  await waitFor(() => assert.equal(cardCI(view).includes("A11:failed"), true));
  await new Promise((resolve) => setTimeout(resolve, CI_SORT_SETTLE_MS * 3));
  assert.deepEqual(cardTitles(view), ["A12", "A10", "A11"]);

  // The last one settles, and the list re-sorts once after a short pause.
  bridge.settle("review-A10", "running");
  await waitFor(() => assert.equal(cardCI(view).includes("A10:running"), true));
  assert.deepEqual(cardTitles(view), ["A12", "A10", "A11"]);
  await waitFor(() => assert.deepEqual(cardTitles(view), ["A11", "A10", "A12"]));
  assert.equal(focusedHandle(), "review-A11");
  assert.deepEqual(cardCI(view), ["A11:failed", "A10:running", "A12:success"]);
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
  state.resolve = (call, status, wait = null) => {
    call.done = true;
    call.settle.resolve({ ci_status: status, retry_after_seconds: wait });
  };
  state.rateLimit = (handle, seconds) => {
    const call = state.open().find((item) => item.params.review === handle);
    assert.ok(call, `no open lookup for ${handle}`);
    state.resolve(call, "unknown", seconds);
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

function reviews(calls) {
  return calls.map((call) => call.params.review);
}

/** Timers the test runs by moving a clock, for retry and rate-limit waits. */
function fakeTimers() {
  const entries = [];
  let clock = 1_000_000;
  return {
    now: () => clock,
    timer: (run, delay) => {
      const entry = { run, at: clock + delay, done: false };
      entries.push(entry);
      return () => {
        entry.done = true;
      };
    },
    advance(ms) {
      clock += ms;
      for (const entry of entries.filter((item) => !item.done && item.at <= clock)) {
        entry.done = true;
        entry.run();
      }
    },
    get scheduled() {
      return entries.filter((entry) => !entry.done).length;
    },
  };
}

/**
 * An IntersectionObserver whose visible review handles the test sets. Each
 * observed card reports once when observed, as a real observer does, and
 * again whenever `show` changes the visible set.
 */
function installObservers() {
  const instances = [];
  let visible = new Set();
  const handleOf = (target) => target.getAttribute?.("data-review-handle") ?? null;
  globalThis.IntersectionObserver = class {
    constructor(callback, options) {
      this.callback = callback;
      this.options = options ?? {};
      this.targets = new Set();
      this.cards = 0;
      this.live = true;
      instances.push(this);
    }
    observe(target) {
      this.targets.add(target);
      if (handleOf(target) !== null) this.cards += 1;
      queueMicrotask(() => {
        if (!this.live || !this.targets.has(target)) return;
        this.callback([{ target, isIntersecting: visible.has(handleOf(target)) }], this);
      });
    }
    unobserve(target) {
      this.targets.delete(target);
    }
    disconnect() {
      this.live = false;
      this.targets.clear();
    }
  };
  return {
    show(handles) {
      visible = new Set(handles);
      for (const observer of instances) {
        if (!observer.live) continue;
        const entries = [...observer.targets]
          .filter((target) => handleOf(target) !== null)
          .map((target) => ({ target, isIntersecting: visible.has(handleOf(target)) }));
        if (entries.length > 0) observer.callback(entries, observer);
      }
    },
    rowOptions() {
      return instances
        .filter((observer) => observer.live && observer.cards > 0)
        .map((observer) => ({ rootMargin: observer.options.rootMargin }));
    },
    uninstall() {
      delete globalThis.IntersectionObserver;
    },
  };
}

function unavailable(view) {
  return [...view.container.querySelectorAll(".review-card")]
    .map((card) => {
      const cell = card.querySelector("[data-ci-unavailable]");
      return cell === null
        ? null
        : `${card.querySelector(".review-title").textContent}:${cell.getAttribute("data-ci-unavailable")}:${cell.getAttribute("title")}`;
    })
    .filter((value) => value !== null);
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
