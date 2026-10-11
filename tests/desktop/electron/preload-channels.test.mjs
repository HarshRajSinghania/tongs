import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { IPC_CHANNELS } from "../../../desktop/dist/src/shared/bridge.js";
import { CI_IPC_CHANNELS } from "../../../desktop/dist/src/shared/ci.js";
import { REVIEW_IPC_CHANNELS } from "../../../desktop/dist/src/shared/review.js";
import { UTILITY_IPC_CHANNELS } from "../../../desktop/dist/src/shared/utilities.js";

// The sandboxed preload cannot import the shared modules, so it redeclares
// every channel name. This reads its source as text and checks each copy.
const preload = readFileSync(
  new URL("../../../desktop/src/preload/index.cts", import.meta.url),
  "utf8",
);

const SHARED = Object.freeze({
  IPC_CHANNELS,
  CI_IPC_CHANNELS,
  REVIEW_IPC_CHANNELS,
  UTILITY_IPC_CHANNELS,
});

function preloadGroup(name) {
  const match = new RegExp(
    `const ${name} = Object\\.freeze\\(\\{([\\s\\S]*?)\\} as const\\);`,
  ).exec(preload);
  assert.ok(match, `${name} is declared in the preload`);
  const entries = [...match[1].matchAll(/([A-Za-z_$][\w$]*)\s*:\s*"([^"]*)"/g)];
  return Object.fromEntries(entries.map(([, key, value]) => [key, value]));
}

function channelLiterals(source) {
  return [...source.matchAll(/["'`](tongs:[^"'`]*)["'`]/g)].map((match) => match[1]);
}

test("each preload channel group equals the shared constants it copies", () => {
  for (const [name, shared] of Object.entries(SHARED)) {
    assert.deepEqual(preloadGroup(name), { ...shared }, `${name} has drifted`);
  }
});

test("the preload's tongs: literals are exactly the shared channel names", () => {
  const literals = channelLiterals(preload);
  const shared = Object.values(SHARED).flatMap((group) => Object.values(group));

  assert.equal(new Set(literals).size, literals.length);
  assert.equal(new Set(shared).size, shared.length);
  assert.deepEqual([...literals].sort(), [...shared].sort());
});

test("the preload exposes the diff context and generation channels", () => {
  assert.equal(preloadGroup("IPC_CHANNELS").getDiffContext, "tongs:diff.context");
  assert.equal(preloadGroup("IPC_CHANNELS").generateDiff, "tongs:diff.generate");
  assert.match(preload, /getDiffContext: \(params: DiffContextParams\): DesktopRead<DiffContextResult> => read\(IPC_CHANNELS\.getDiffContext, params\)/);
  assert.match(preload, /generateDiff: \(params: GenerateDiffParams\): DesktopRead<GenerateDiffResult> => read\(IPC_CHANNELS\.generateDiff, params\)/);
});
