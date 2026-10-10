import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { readFileSync } from "node:fs";
import { recoverRendererSession } from "../../../desktop/dist/src/main/renderer_lifecycle.js";
import {
  ServiceStatusPublisher,
  SidecarTransport,
} from "../../../desktop/dist/src/main/sidecar.js";
import {
  SERVICE_STATUS_TEXT,
  ServiceNotices,
  ServiceStatusLine,
  ServiceStatusModel,
  isResyncEvent,
} from "../../../desktop/dist/src/renderer/core/service-status.js";
import { harness, launch, transportFor } from "../electron/fake-sidecar.mjs";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const React = desktopRequire("react");
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
const { act, cleanup, fireEvent, render } = desktopRequire("@testing-library/react");
afterEach(cleanup);

/**
 * Connects a real transport over a fake service to the header through the same
 * path the desktop uses: the main-process publisher sends each change and
 * answers the current-state query, and the renderer bridge relays both.
 */
function wire(transport) {
  const listeners = new Set();
  const publisher = new ServiceStatusPublisher(transport, (status) => {
    const copy = structuredClone(status);
    for (const listener of listeners) listener(copy);
  });
  publisher.start();
  const bridge = {
    getServiceStatus: async () => structuredClone(publisher.current),
    onServiceStatus: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  return { bridge, publisher, listeners };
}

function headerText() {
  return document.querySelector("#service-status")?.textContent ?? null;
}

function headerClass() {
  return document.querySelector("#service-status")?.className ?? null;
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setImmediate(resolve));
  });
}

test("header changes from connected to not running when the service stops, and back after a restart", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { bridge, publisher, listeners } = wire(transport);
  const model = new ServiceStatusModel();
  const view = render(
    React.createElement(ServiceStatusLine, { model, bridge }),
  );
  await settle();
  assert.equal(headerText(), "Local service connected");
  assert.equal(headerClass(), "service-status service-status-ready");

  await act(async () => {
    fake.children[0].finish(1, null);
  });
  assert.equal(headerText(), SERVICE_STATUS_TEXT.stopped);
  assert.equal(headerClass(), "service-status service-status-error");

  await act(async () => {
    await transport.restart();
  });
  assert.equal(headerText(), "Local service connected");
  assert.equal(headerClass(), "service-status service-status-ready");

  view.unmount();
  assert.equal(listeners.size, 0);
  publisher.dispose();
  await transport.stop();
});

test("header shows not running when the connection fails on a bad frame", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { bridge, publisher } = wire(transport);
  const model = new ServiceStatusModel();
  render(React.createElement(ServiceStatusLine, { model, bridge }));
  await settle();
  assert.equal(headerText(), "Local service connected");
  const pending = transport.requestRead("assets.list", {}).result;
  await act(async () => {
    fake.children[0].stdout.write("not json\n");
    await assert.rejects(pending, { code: "invalid_frame" });
  });
  assert.equal(headerText(), SERVICE_STATUS_TEXT.stopped);
  publisher.dispose();
  await transport.stop();
});

test("a header mounted after the service stopped starts at not running", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { bridge, publisher } = wire(transport);
  fake.children[0].finish(1, null);
  const model = new ServiceStatusModel();
  model.probeSucceeded();
  render(React.createElement(ServiceStatusLine, { model, bridge }));
  await settle();
  assert.equal(headerText(), SERVICE_STATUS_TEXT.stopped);
  publisher.dispose();
  await transport.stop();
});

test("a stale current-state answer cannot undo a newer stop", async () => {
  const model = new ServiceStatusModel();
  model.applyStatus({ state: "stopped", revision: 4 });
  model.applyStatus({ state: "connected", revision: 3 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.stopped);
  model.applyStatus({ state: "connected", revision: 5 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connected);
});

test("not running outranks the updates notice, and a reconnect clears it", () => {
  const model = new ServiceStatusModel();
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connecting);
  model.applyStatus({ state: "connected", revision: 1 });
  model.updatesAvailable();
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.updates);
  model.applyStatus({ state: "stopped", revision: 2 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.stopped);
  model.applyStatus({ state: "connected", revision: 3 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connected);
});

test("the stopped header tells the user how to recover", () => {
  const model = new ServiceStatusModel();
  model.applyStatus({ state: "stopped", revision: 1 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.stopped);
  assert.equal(model.view.text.startsWith("Local service not running"), true);
  assert.equal(model.view.text.includes("Quit and reopen Tongs"), true);
});

test("the header says the service is restarting, then names the fix if it gives up", () => {
  const model = new ServiceStatusModel();
  model.applyStatus({ state: "connected", revision: 1, notices: [] });
  model.applyStatus({ state: "stopped", revision: 2, notices: [], recovery: "restarting" });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.restarting);
  assert.equal(model.view.text, "Local service stopped. Restarting it…");
  assert.equal(model.view.className, "service-status service-status-warning");
  model.applyStatus({ state: "stopped", revision: 3, notices: [], recovery: "failed" });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.failed);
  assert.equal(model.view.text.includes("Quit and reopen Tongs"), true);
  assert.equal(model.view.className, "service-status service-status-error");
  for (const text of Object.values(SERVICE_STATUS_TEXT)) {
    assert.equal(text.includes("\u2014"), false);
  }
});

test("a connected service ignores the recovery report and an unknown one reads as stopped", () => {
  const model = new ServiceStatusModel();
  model.applyStatus({ state: "connected", revision: 1, notices: [], recovery: "restarting" });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connected);
  model.applyStatus({ state: "stopped", revision: 2, notices: [], recovery: "rebooting" });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.stopped);
  model.applyStatus({ state: "stopped", revision: 3, notices: [], recovery: 7 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.stopped);
});

test("the header follows the main-process recovery through a restart", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { bridge, publisher } = wire(transport);
  render(React.createElement(ServiceStatusLine, { model: new ServiceStatusModel(), bridge }));
  await settle();
  assert.equal(headerText(), SERVICE_STATUS_TEXT.connected);
  await act(async () => {
    fake.children[0].finish(1, null);
    transport.setRecovery("restarting");
  });
  assert.equal(headerText(), SERVICE_STATUS_TEXT.restarting);
  await act(async () => {
    await transport.restart();
    transport.setRecovery("idle");
  });
  assert.equal(headerText(), SERVICE_STATUS_TEXT.connected);
  await act(async () => {
    await transport.restart();
  });
  // A header mounted after the recovery gave up starts at the fix.
  await act(async () => {
    fake.children[2].finish(1, null);
    transport.setRecovery("failed");
  });
  cleanup();
  render(React.createElement(ServiceStatusLine, { model: new ServiceStatusModel(), bridge }));
  await settle();
  assert.equal(headerText(), SERVICE_STATUS_TEXT.failed);
  assert.equal(headerClass(), "service-status service-status-error");
  publisher.dispose();
  await transport.stop();
});

test("only a resync warns that updates are available", () => {
  assert.equal(isResyncEvent("protocol.resync_required", {}), true);
  assert.equal(
    isResyncEvent("service.changed", { kind: "resync_required" }),
    true,
  );
  for (const kind of [
    "review_changed",
    "pipeline_changed",
    "repositories_changed",
    "draft_changed",
  ]) {
    assert.equal(isResyncEvent("service.changed", { kind }), false, kind);
  }
  assert.equal(isResyncEvent("service.changed", null), false);
  assert.equal(isResyncEvent("service.changed", "resync_required"), false);
  assert.equal(isResyncEvent("plugin.event", { kind: "resync_required" }), false);
});

test("clearing the updates notice restores the connection text", () => {
  const model = new ServiceStatusModel();
  let changes = 0;
  model.subscribe(() => {
    changes += 1;
  });
  model.applyStatus({ state: "connected", revision: 1 });
  model.updatesAvailable();
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.updates);
  assert.equal(model.view.className, "service-status service-status-warning");
  model.clearUpdates();
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connected);
  assert.equal(changes, 3);
  // Clearing again, or while stopped, changes nothing the header shows.
  model.clearUpdates();
  assert.equal(changes, 3);
  model.applyStatus({ state: "stopped", revision: 2 });
  model.clearUpdates();
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.stopped);
  assert.equal(changes, 4);
});

test("malformed status reports are ignored", () => {
  const model = new ServiceStatusModel();
  model.applyStatus({ state: "exploded", revision: 1 });
  model.applyStatus({ state: "stopped", revision: -1 });
  model.applyStatus(null);
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connecting);
});

test("after a failed recovery the reloaded header says not running", async () => {
  const fake = harness({ autoHandshake: false });
  // Every service started after the first exits before its handshake.
  const spawn = (...args) => {
    const child = fake.spawn(...args);
    if (fake.children.length > 1) queueMicrotask(() => child.finish(1, null));
    return child;
  };
  const transport = new SidecarTransport(launch, 1_000, 1_000, 1_000, spawn);
  const started = transport.start();
  await new Promise((resolve) => setImmediate(resolve));
  fake.respond(fake.children[0]);
  await started;
  const { bridge, publisher } = wire(transport);
  render(
    React.createElement(ServiceStatusLine, { model: new ServiceStatusModel(), bridge }),
  );
  await settle();
  assert.equal(headerText(), "Local service connected");

  const steps = [];
  let recovered;
  await act(async () => {
    recovered = await recoverRendererSession({
      resetBindings: () => steps.push("reset"),
      restartSidecar: async () => {
        steps.push("restart");
        await transport.restart();
      },
      refreshAssets: async () => steps.push("assets"),
      // A reload replaces the document, so the header mounts again from the
      // main-process state.
      loadDocument: async () => {
        steps.push("load");
        cleanup();
        render(
          React.createElement(ServiceStatusLine, {
            model: new ServiceStatusModel(),
            bridge,
          }),
        );
      },
      showFailure: async () => steps.push("failure"),
    });
  });
  await settle();
  assert.equal(recovered, false);
  assert.deepEqual(steps, ["reset", "restart", "load", "failure"]);
  assert.equal(transport.serviceState, "stopped");
  assert.equal(headerText(), SERVICE_STATUS_TEXT.stopped);
  assert.equal(headerClass(), "service-status service-status-error");
  assert.equal(document.querySelectorAll("#service-status").length, 1);
  publisher.dispose();
  await transport.stop();
});

test("the main process leaves the header text to the status channel", () => {
  const source = readFileSync(
    new URL("../../../desktop/src/main/index.ts", import.meta.url),
    "utf8",
  );
  assert.equal(source.includes("#service-status"), false);
  assert.equal(source.includes("service is unavailable"), false);
});

function noticeTexts() {
  return [...document.querySelectorAll("#service-notices .service-notice span")].map(
    (node) => node.textContent,
  );
}

test("startup recovery warnings show as notices until dismissed", async () => {
  const warnings = [
    "Part of the review may already have posted. Review: github.com/acme/widgets #12.",
    "Part of the review may already have posted. Review: gitlab.example.com/group/app #3.",
  ];
  const fake = harness({ handshakeExtras: { recovery_warnings: warnings } });
  const transport = transportFor(fake);
  await transport.start();
  const { bridge, publisher } = wire(transport);
  const model = new ServiceStatusModel();
  render(
    React.createElement(
      React.Fragment,
      null,
      React.createElement(ServiceStatusLine, { model, bridge }),
      React.createElement(ServiceNotices, { model }),
    ),
  );
  await settle();
  assert.equal(headerText(), "Local service connected");
  assert.deepEqual(noticeTexts(), warnings);
  assert.equal(
    document.querySelectorAll("#service-notices [role='alert']").length,
    2,
  );

  const dismiss = document.querySelectorAll("#service-notices button")[0];
  assert.equal(dismiss.textContent, "Dismiss");
  await act(async () => {
    fireEvent.click(dismiss);
  });
  assert.deepEqual(noticeTexts(), [warnings[1]]);

  // A later report of the same session does not bring a dismissed one back.
  await act(async () => {
    model.applyStatus({ state: "connected", revision: 99, notices: warnings });
  });
  assert.deepEqual(noticeTexts(), [warnings[1]]);

  await act(async () => {
    fireEvent.click(document.querySelectorAll("#service-notices button")[0]);
  });
  assert.equal(document.querySelectorAll("#service-notices").length, 0);
  publisher.dispose();
  await transport.stop();
});

test("a stopped service keeps the notices and malformed notices are ignored", () => {
  const model = new ServiceStatusModel();
  model.applyStatus({ state: "connected", revision: 1, notices: ["Check review 4."] });
  assert.deepEqual([...model.notices()], ["Check review 4."]);
  model.applyStatus({ state: "stopped", revision: 2, notices: [] });
  assert.deepEqual([...model.notices()], ["Check review 4."]);
  model.applyStatus({ state: "connected", revision: 3, notices: "Check review 4." });
  assert.deepEqual([...model.notices()], ["Check review 4."]);
  model.applyStatus({ state: "connected", revision: 4, notices: [7, "", "Check review 5."] });
  assert.deepEqual([...model.notices()], ["Check review 4.", "Check review 5."]);
  model.dismissNotice("Check review 4.");
  model.applyStatus({ state: "connected", revision: 5, notices: ["Check review 4."] });
  assert.deepEqual([...model.notices()], ["Check review 5."]);
});

test("a recovery notice survives a service restart that reports nothing until dismissed", async () => {
  const warning =
    "Part of the review may already have posted. Review: github.com/acme/widgets #12.";
  const extras = { recovery_warnings: [warning] };
  const fake = harness({ handshakeExtras: extras });
  const transport = transportFor(fake);
  await transport.start();
  const { bridge, publisher } = wire(transport);
  const model = new ServiceStatusModel();
  render(
    React.createElement(
      React.Fragment,
      null,
      React.createElement(ServiceStatusLine, { model, bridge }),
      React.createElement(ServiceNotices, { model }),
    ),
  );
  await settle();
  assert.deepEqual(noticeTexts(), [warning]);

  // The store warns only once: the restarted service reports no warnings.
  delete extras.recovery_warnings;
  await act(async () => {
    fake.children[0].finish(9, null);
  });
  await settle();
  assert.equal(headerText(), SERVICE_STATUS_TEXT.stopped);
  assert.deepEqual(noticeTexts(), [warning]);
  await act(async () => {
    await transport.restart();
  });
  await settle();
  assert.equal(fake.children.length, 2);
  assert.equal(headerText(), SERVICE_STATUS_TEXT.connected);
  assert.deepEqual(noticeTexts(), [warning]);
  assert.deepEqual(publisher.current.notices, [warning]);

  // A renderer that starts after the restart still receives it.
  const fresh = new ServiceStatusModel();
  fresh.applyStatus(await bridge.getServiceStatus());
  assert.deepEqual([...fresh.notices()], [warning]);

  await act(async () => {
    fireEvent.click(document.querySelectorAll("#service-notices button")[0]);
  });
  assert.equal(document.querySelectorAll("#service-notices").length, 0);
  publisher.dispose();
  await transport.stop();
});
