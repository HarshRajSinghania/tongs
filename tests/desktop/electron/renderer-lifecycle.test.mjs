import assert from "node:assert/strict";
import test from "node:test";
import { EventEmitter } from "node:events";
import {
  AUTOMATIC_RESTART_WINDOW_MS,
  MAX_AUTOMATIC_RESTARTS,
  recoverRendererSession,
  RendererRecoveryCoordinator,
  ServiceRecoveryPolicy,
  watchServiceLoss,
} from "../../../desktop/dist/src/main/renderer_lifecycle.js";

test("renderer recovery reloads a visible safe failure after restart rejection", async () => {
  const calls = [];
  const recovered = await recoverRendererSession({
    resetBindings: () => calls.push("reset"),
    restartSidecar: async () => { calls.push("restart"); throw new Error("secret"); },
    refreshAssets: async () => calls.push("assets"),
    loadDocument: async () => calls.push("load"),
    showFailure: async () => calls.push("failure"),
  });
  assert.equal(recovered, false);
  assert.deepEqual(calls, ["reset", "restart", "load", "failure"]);
});

test("renderer recovery queues a crash received while a reload is completing", async () => {
  const calls = [];
  let releaseFirstLoad;
  const firstLoadBlocked = new Promise((resolve) => {
    releaseFirstLoad = resolve;
  });
  const coordinator = new RendererRecoveryCoordinator({
    resetBindings: () => calls.push("reset"),
    restartSidecar: async () => calls.push("restart"),
    refreshAssets: async () => calls.push("assets"),
    loadDocument: async () => {
      calls.push("load");
      if (calls.filter((call) => call === "load").length === 1) {
        await firstLoadBlocked;
      }
    },
    showFailure: async () => calls.push("failure"),
  });

  const reload = coordinator.request();
  await new Promise((resolve) => setImmediate(resolve));
  const crash = coordinator.request();
  releaseFirstLoad();
  await Promise.all([reload, crash]);

  assert.deepEqual(calls, [
    "reset",
    "restart",
    "assets",
    "load",
    "reset",
    "restart",
    "assets",
    "load",
  ]);
});

/** A policy over a scripted restart, a settable clock and a quit flag. */
function scripted({ outcomes = [], quitting = () => false } = {}) {
  const clock = { now: 0 };
  const states = [];
  let restarts = 0;
  const policy = new ServiceRecoveryPolicy({
    restart: async () => {
      restarts += 1;
      const outcome = outcomes.length > 0 ? outcomes.shift() : true;
      if (outcome instanceof Error) throw outcome;
      return outcome;
    },
    shuttingDown: quitting,
    onState: (state) => states.push(state),
    now: () => clock.now,
  });
  return { policy, clock, states, restarts: () => restarts };
}

test("service recovery allows three restarts a minute, then stays stopped", async () => {
  assert.equal(MAX_AUTOMATIC_RESTARTS, 3);
  assert.equal(AUTOMATIC_RESTART_WINDOW_MS, 60_000);
  const { policy, clock, states, restarts } = scripted();
  for (const at of [0, 10_000, 20_000]) {
    clock.now = at;
    await policy.serviceLost();
    assert.equal(policy.state, "idle");
  }
  clock.now = 30_000;
  await policy.serviceLost();
  assert.equal(policy.state, "failed");
  assert.equal(restarts(), 3);
  // Once it gave up, later reports never restart it, even after the window.
  clock.now = 500_000;
  await policy.serviceLost();
  assert.equal(restarts(), 3);
  assert.equal(policy.restartCount, 3);
  assert.deepEqual(states, [
    "restarting", "idle",
    "restarting", "idle",
    "restarting", "idle",
    "failed",
  ]);
});

test("restarts older than the window no longer count toward the limit", async () => {
  const { policy, clock, restarts } = scripted();
  // At 70 s the restarts at 0 s and 10 s have left the window.
  for (const at of [0, 10_000, 20_000, 70_000, 80_000, 90_000]) {
    clock.now = at;
    await policy.serviceLost();
  }
  assert.equal(policy.state, "idle");
  assert.equal(restarts(), 6);
  clock.now = 95_000;
  await policy.serviceLost();
  assert.equal(policy.state, "failed");
  assert.equal(restarts(), 6);
});

test("a failed restart is retried until the limit, and a thrown one counts as failed", async () => {
  const { policy, states, restarts } = scripted({
    outcomes: [false, new Error("handshake failed"), false, true],
  });
  await policy.serviceLost();
  assert.equal(restarts(), 3);
  assert.equal(policy.state, "failed");
  assert.deepEqual(states, ["restarting", "failed"]);
});

test("a restart that succeeds after one failure returns to idle", async () => {
  const { policy, states, restarts } = scripted({ outcomes: [false, true] });
  await policy.serviceLost();
  assert.equal(restarts(), 2);
  assert.equal(policy.state, "idle");
  assert.deepEqual(states, ["restarting", "idle"]);
});

test("reports during a restart join it instead of starting another", async () => {
  let release;
  const blocked = new Promise((resolve) => {
    release = resolve;
  });
  let restarts = 0;
  const policy = new ServiceRecoveryPolicy({
    restart: async () => {
      restarts += 1;
      await blocked;
      return true;
    },
    shuttingDown: () => false,
  });
  const first = policy.serviceLost();
  const second = policy.serviceLost();
  await new Promise((resolve) => setImmediate(resolve));
  const third = policy.serviceLost();
  assert.equal(policy.state, "restarting");
  release();
  await Promise.all([first, second, third]);
  assert.equal(restarts, 1);
  assert.equal(policy.state, "idle");
});

test("service recovery does nothing while the app shuts down", async () => {
  let quitting = true;
  const { policy, states, restarts } = scripted({ quitting: () => quitting });
  await policy.serviceLost();
  assert.equal(restarts(), 0);
  assert.deepEqual(states, []);
  // A quit that starts during a failed restart stops the retries.
  quitting = false;
  const late = scripted({
    outcomes: [false, false],
    quitting: () => late.restarts() >= 1,
  });
  await late.policy.serviceLost();
  assert.equal(late.restarts(), 1);
  assert.equal(late.policy.state, "idle");
});

test("a connection made outside the policy clears a give-up", async () => {
  const { policy, restarts } = scripted({ outcomes: [false, false, false, true] });
  await policy.serviceLost();
  assert.equal(policy.state, "failed");
  policy.serviceConnected();
  assert.equal(policy.state, "idle");
  await policy.serviceLost();
  assert.equal(policy.state, "failed");
  assert.equal(restarts(), 3);
});

test("the watcher sends crashes and not-running reads to the policy and detaches", async () => {
  const source = new EventEmitter();
  const { policy, restarts } = scripted();
  const detach = watchServiceLoss(source, policy);
  source.emit("crash", new Error("unexpected_eof"));
  await policy.serviceLost();
  source.emit("not_running");
  await policy.serviceLost();
  assert.equal(restarts(), 2);
  detach();
  assert.equal(source.listenerCount("crash"), 0);
  assert.equal(source.listenerCount("not_running"), 0);
  assert.equal(source.listenerCount("status"), 0);
});

test("recovery limits must be positive", () => {
  for (const options of [{ maxRestarts: -1 }, { windowMs: 0 }, { maxRestarts: 1.5 }]) {
    assert.throws(
      () =>
        new ServiceRecoveryPolicy({
          restart: async () => true,
          shuttingDown: () => false,
          ...options,
        }),
      /limits must be positive/,
    );
  }
});
