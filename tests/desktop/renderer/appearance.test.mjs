import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import test, { afterEach } from "node:test";

import {
  AppearanceController,
  DESKTOP_SETTINGS_FILE,
  DesktopSettingsStore,
} from "../../../desktop/dist/src/main/appearance.js";
import {
  COLOR_SCHEME_LABELS,
  COLOR_SCHEME_SAVE_FAILED,
  ColorSchemeToggle,
  colorSchemeTitle,
  newerAppearance,
  selectedColorScheme,
} from "../../../desktop/dist/src/renderer/core/appearance.js";
import { settle, waitForText } from "./wait.mjs";

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
afterEach(() => {
  cleanup();
  delete document.documentElement.dataset.colorScheme;
});

function fakeTheme() {
  return {
    themeSource: "system",
    get shouldUseDarkColors() {
      return this.themeSource === "dark";
    },
  };
}

/**
 * Connects the toggle to a real main-process controller the way the IPC
 * handlers and preload do: structured copies both ways, and the push channel
 * relays every change.
 */
async function wire(context, configured = "system") {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-appearance-ui-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const file = path.join(root, DESKTOP_SETTINGS_FILE);
  const theme = fakeTheme();
  const controller = new AppearanceController(new DesktopSettingsStore(file), theme);
  await controller.load();
  controller.setConfigured(configured);
  const chosen = [];
  const bridge = {
    getAppearance: async () => structuredClone(controller.current),
    setColorScheme: async (choice) => {
      chosen.push(choice);
      return structuredClone(await controller.choose(choice));
    },
    onAppearance: (listener) =>
      controller.onChange((appearance) => listener(structuredClone(appearance))),
  };
  return { bridge, controller, theme, file, chosen };
}

function pressed(container) {
  return [...container.querySelectorAll("[data-color-scheme-choice]")].map(
    (button) => `${button.dataset.colorSchemeChoice}:${button.getAttribute("aria-pressed")}`,
  );
}

function rootScheme() {
  return document.documentElement.dataset.colorScheme ?? "";
}

function choice(container, scheme) {
  return container.querySelector(`[data-color-scheme-choice="${scheme}"]`);
}

test("the toggle offers System, Light and Dark and starts on System", async (context) => {
  const { bridge } = await wire(context);
  const view = render(React.createElement(ColorSchemeToggle, { bridge }));
  await waitForText(rootScheme, "system", "root scheme");
  assert.deepEqual(pressed(view.container), [
    "system:true",
    "light:false",
    "dark:false",
  ]);
  assert.equal(view.container.textContent, "SystemLightDark");
  assert.equal(
    view.container.querySelector(".color-scheme-toggle").getAttribute("aria-label"),
    "Colour scheme",
  );
});

test("choosing Dark applies at once, marks the root and is saved", async (context) => {
  const { bridge, theme, file, chosen } = await wire(context);
  const view = render(React.createElement(ColorSchemeToggle, { bridge }));
  await waitForText(rootScheme, "system", "root scheme");
  await act(async () => {
    fireEvent.click(choice(view.container, "dark"));
  });
  await waitForText(rootScheme, "dark", "root scheme");
  assert.deepEqual(pressed(view.container), [
    "system:false",
    "light:false",
    "dark:true",
  ]);
  assert.deepEqual(chosen, ["dark"]);
  assert.equal(theme.themeSource, "dark");
  assert.equal(await readFile(file, "utf8"), '{"color_scheme":"dark"}\n');
});

test("choosing System falls back to the configured scheme", async (context) => {
  const { bridge, theme } = await wire(context, "light");
  const view = render(React.createElement(ColorSchemeToggle, { bridge }));
  await waitForText(rootScheme, "light", "root scheme");
  assert.equal(theme.themeSource, "light");
  assert.equal(
    choice(view.container, "system").title,
    "Use the config.toml colour scheme (light)",
  );
  await act(async () => {
    fireEvent.click(choice(view.container, "dark"));
  });
  await waitForText(rootScheme, "dark", "root scheme");
  await act(async () => {
    fireEvent.click(choice(view.container, "system"));
  });
  await waitForText(rootScheme, "light", "root scheme");
  assert.equal(theme.themeSource, "light");
  assert.deepEqual(pressed(view.container), [
    "system:true",
    "light:false",
    "dark:false",
  ]);
});

test("a saved choice is pressed again after a restart", async (context) => {
  const first = await wire(context);
  await first.controller.choose("light");
  const theme = fakeTheme();
  const restarted = new AppearanceController(
    new DesktopSettingsStore(first.file),
    theme,
  );
  await restarted.load();
  const bridge = {
    getAppearance: async () => structuredClone(restarted.current),
    setColorScheme: async (scheme) => structuredClone(await restarted.choose(scheme)),
    onAppearance: (listener) => restarted.onChange(listener),
  };
  const view = render(React.createElement(ColorSchemeToggle, { bridge }));
  await waitForText(rootScheme, "light", "root scheme");
  assert.deepEqual(pressed(view.container), [
    "system:false",
    "light:true",
    "dark:false",
  ]);
  assert.equal(theme.themeSource, "light");
});

test("a configured scheme pushed after a service restart reaches the root", async (context) => {
  const { bridge, controller } = await wire(context);
  render(React.createElement(ColorSchemeToggle, { bridge }));
  await waitForText(rootScheme, "system", "root scheme");
  await act(async () => {
    controller.setConfigured("dark");
  });
  await waitForText(rootScheme, "dark", "root scheme");
});

test("a failed save shows a short notice and keeps the scheme", async () => {
  const appearance = { configured: "system", saved: null, effective: "system", revision: 0 };
  const bridge = {
    getAppearance: async () => appearance,
    setColorScheme: async () => {
      throw new Error("The colour scheme could not be saved.");
    },
    onAppearance: () => () => undefined,
  };
  const view = render(React.createElement(ColorSchemeToggle, { bridge }));
  await waitForText(rootScheme, "system", "root scheme");
  await act(async () => {
    fireEvent.click(choice(view.container, "dark"));
  });
  await waitForText(
    () => view.container.querySelector(".color-scheme-error")?.textContent ?? "",
    COLOR_SCHEME_SAVE_FAILED,
    "save failure notice",
  );
  assert.equal(rootScheme(), "system");
  assert.deepEqual(pressed(view.container), [
    "system:true",
    "light:false",
    "dark:false",
  ]);
});

test("an unavailable appearance leaves nothing pressed and the root unmarked", async () => {
  const bridge = {
    getAppearance: async () => {
      throw new Error("No handler registered");
    },
    setColorScheme: async () => {
      throw new Error("No handler registered");
    },
    onAppearance: () => () => undefined,
  };
  const view = render(React.createElement(ColorSchemeToggle, { bridge }));
  await settle();
  assert.deepEqual(pressed(view.container), [
    "system:false",
    "light:false",
    "dark:false",
  ]);
  assert.equal(rootScheme(), "");
});

test("a malformed or older appearance is ignored", () => {
  const current = { configured: "system", saved: "dark", effective: "dark", revision: 4 };
  assert.equal(newerAppearance(current, { ...current, saved: "light", revision: 3 }), current);
  assert.equal(newerAppearance(current, { ...current, effective: "light", revision: 5 }), current);
  assert.equal(newerAppearance(current, "dark"), current);
  assert.equal(newerAppearance(null, null), null);
  const next = { configured: "system", saved: null, effective: "system", revision: 5 };
  assert.equal(newerAppearance(current, next), next);
});

test("the toggle labels and titles say what each choice does", () => {
  assert.deepEqual({ ...COLOR_SCHEME_LABELS }, { system: "System", light: "Light", dark: "Dark" });
  assert.equal(colorSchemeTitle("system", null), "Follow the system colour scheme");
  assert.equal(colorSchemeTitle("light", null), "Always use the light colour scheme");
  assert.equal(colorSchemeTitle("dark", null), "Always use the dark colour scheme");
  const configured = { configured: "dark", saved: null, effective: "dark", revision: 1 };
  assert.equal(colorSchemeTitle("system", configured), "Use the config.toml colour scheme (dark)");
  assert.equal(selectedColorScheme(configured), "system");
  assert.equal(selectedColorScheme({ ...configured, saved: "light", effective: "light" }), "light");
});

test("the stylesheet pins color-scheme to the marked root in each forced scheme", async () => {
  const css = (
    await readFile(
      path.resolve(import.meta.dirname, "../../../desktop/dist/shell/styles.css"),
      "utf8",
    )
  ).replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (scheme) =>
    new RegExp(
      `:root\\[data-color-scheme="${scheme}"\\]\\s*\\{\\s*color-scheme:\\s*${scheme};\\s*\\}`,
    ).test(css);
  assert.equal(rule("light"), true);
  assert.equal(rule("dark"), true);
  assert.equal(/@media\s*\(\s*prefers-color-scheme\s*:\s*light\s*\)/.test(css), true);
});
