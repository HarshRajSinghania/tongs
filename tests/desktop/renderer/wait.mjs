// Shared bounded waits for the desktop renderer tests (issue #220).
//
// A renderer test that reads rendered state right after an event can run
// before async state (a capability read, a search result, a draft save) has
// settled. Every such read goes through one of the helpers below, which retry
// until the value matches or the bounded timeout from `setup.mjs` runs out.
//
// The helpers compare only strings, counts and booleans, and they fail with a
// short message that names the read, the expected value and the last value
// seen. A read that returns a DOM node or any other object fails at once, so a
// failure never formats a jsdom tree, which is what pushed failing waits past
// the memory limit of the bounded test units.
//
// The file name must not match `*.test.mjs`, so the desktop test glob loads it
// only as a module and never runs it as a test file. Like `setup.mjs`, it must
// not load the Testing Library entry points when it is imported: each test
// file installs its jsdom globals after its imports run, so `waitFor` is
// resolved on first use instead.
import { createRequire } from "node:module";

import { ASYNC_UTIL_TIMEOUT_MS } from "./setup.mjs";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);

/**
 * Every sentence a capability-gated control shows as its title while the
 * capability read it depends on is still in flight. Each is pinned to the
 * rendered title by a string assertion in the renderer tests, so a reword in
 * the source fails those pins instead of making `capabilitiesReady` pass
 * while a control is still loading.
 */
export const CAPABILITY_LOADING = Object.freeze([
  "General comment support for this review is still loading.",
  "Inline comment support for this review is still loading.",
  "Reply support for this review is still loading.",
  "Resolution support for this review is still loading.",
  "Verdict support for this review is still loading.",
  "Review action support for this review is still loading.",
]);

const CONTROLS = "button, input, select, textarea";
const MAX_SHOWN = 120;

let testingLibrary = null;

function waitForImpl() {
  testingLibrary ??= desktopRequire("@testing-library/react");
  return testingLibrary.waitFor;
}

function shorten(text) {
  const flat = String(text).replace(/\s+/g, " ");
  return flat.length > MAX_SHOWN ? `${flat.slice(0, MAX_SHOWN)}...` : flat;
}

function show(value) {
  return typeof value === "string" ? JSON.stringify(shorten(value)) : String(value);
}

function primitive(value) {
  return (
    value === null ||
    value === undefined ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  );
}

class WaitError extends Error {
  constructor(message) {
    super(message);
    this.name = "WaitError";
  }
}

/** Thrown by a read that can never match, which ends the wait at once. */
class FatalRead extends Error {}

/**
 * Retries `check` until it returns null or the bounded timeout runs out.
 * `check` returns a short description of what is still wrong, or throws while
 * the surface is still rendering (a query that finds nothing yet), which
 * counts the same. A timeout fails with `name` and the last description only.
 */
async function waitUntil(name, check) {
  let last = "nothing";
  let fatal = null;
  await waitForImpl()(
    () => {
      let state;
      try {
        state = check();
      } catch (error) {
        if (error instanceof FatalRead) {
          fatal = error.message;
          return;
        }
        last = `error ${show(error?.message ?? error)}`;
        throw new WaitError(last);
      }
      if (state === null) return;
      last = state;
      throw new WaitError(last);
    },
    {
      timeout: ASYNC_UTIL_TIMEOUT_MS,
      onTimeout: () =>
        new WaitError(
          `${name}: ${last} after ${ASYNC_UTIL_TIMEOUT_MS} ms`,
        ),
    },
  );
  if (fatal !== null) throw new WaitError(`${name}: ${fatal}`);
}

/**
 * Retries `read` until it returns exactly `expected`. `label` names the wait
 * in the failure message and defaults to the read's own source.
 */
async function waitForValue(read, expected, label) {
  await waitUntil(shorten(label ?? read.toString()), () => {
    const value = read();
    if (!primitive(value))
      // Stop at once: formatting an object, above all a DOM node, is the
      // allocation this module exists to avoid.
      throw new FatalRead(
        `the read returned ${typeof value}, not a string, count or boolean`,
      );
    return value === expected
      ? null
      : `expected ${show(expected)}, last saw ${show(value)}`;
  });
}

/**
 * Retries `read` until it returns an array equal to `expected`, compared as
 * JSON. Both may nest arrays, but every leaf must be a string, number,
 * boolean or null, so a mismatch is reported as short text.
 */
async function waitForArray(read, expected, label) {
  const want = JSON.stringify(expected);
  await waitUntil(shorten(label ?? read.toString()), () => {
    const value = read();
    if (!Array.isArray(value) || !value.flat(Infinity).every(primitive))
      throw new FatalRead(
        "the read returned something other than an array of strings, counts or booleans",
      );
    const got = JSON.stringify(value);
    return got === want
      ? null
      : `expected ${shorten(want)}, last saw ${shorten(got)}`;
  });
}

/** Waits until `read()` returns the string `expected`. */
export async function waitForText(read, expected, label) {
  if (typeof expected !== "string")
    throw new TypeError("waitForText expects a string");
  await waitForValue(read, expected, label);
}

/** Waits until `read()` returns the count `expected`. */
export async function waitForCount(read, expected, label) {
  if (!Number.isInteger(expected) || expected < 0)
    throw new TypeError("waitForCount expects a non-negative integer");
  await waitForValue(read, expected, label);
}

/** Waits until `read()` returns the boolean `expected`. */
export async function waitForFlag(read, expected, label) {
  if (typeof expected !== "boolean")
    throw new TypeError("waitForFlag expects a boolean");
  await waitForValue(read, expected, label);
}

/**
 * Waits until `read()` returns a list of strings, counts or booleans (or
 * nested lists of them) equal to `expected`.
 */
export async function waitForList(read, expected, label) {
  if (!Array.isArray(expected) || !expected.flat(Infinity).every(primitive))
    throw new TypeError("waitForList expects an array of strings, counts or booleans");
  await waitForArray(read, expected, label);
}

/**
 * Waits until the one button named `name` is enabled, then returns it for the
 * caller to press. The wait itself reads only the button's `disabled` flag.
 */
export async function enabledButton(view, name) {
  await waitForFlag(
    () => view.getByRole("button", { name }).disabled,
    false,
    `button ${show(name)} disabled`,
  );
  return view.getByRole("button", { name });
}

/**
 * Waits until every capability-gated control on the surface has an answer
 * behind it. A control is disabled both while its read is in flight and for
 * its own standing reason, so waiting on `disabled` cannot tell the two apart
 * and leaves a loading sentence readable as if it were the reason: that is the
 * race #215 caught on a slow runner.
 *
 * `gated` is a CSS selector for the gated controls the caller is about to
 * read. It must match at least one button or input, so the wait cannot pass
 * vacuously on a surface that rendered none of them, and no button or input
 * anywhere in `container` may still show a loading sentence. Counted rather
 * than matched against a node, so the wait can only end when no control is
 * still loading.
 */
export async function capabilitiesReady(container, gated) {
  if (typeof gated !== "string" || gated === "")
    throw new TypeError("capabilitiesReady expects a selector for the gated controls");
  await waitUntil(`capabilities for ${shorten(gated)}`, () => {
    const present = [...container.querySelectorAll(gated)].filter((element) =>
      element.matches(CONTROLS),
    ).length;
    const loading = [...container.querySelectorAll(CONTROLS)].filter(
      (element) => CAPABILITY_LOADING.includes(element.title),
    ).length;
    return present > 0 && loading === 0
      ? null
      : `${present} gated controls, ${loading} still loading`;
  });
}

/**
 * Lets every already-dispatched read and its follow-on effects run, for a test
 * that proves something does not happen (no second read, no save). It checks
 * nothing itself; a test waiting for something to happen uses a wait above.
 */
export async function settle(rounds = 20) {
  for (let round = 0; round < rounds; round += 1)
    await new Promise((resolve) => setTimeout(resolve, 5));
}
