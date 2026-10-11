import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  COLOR_SCHEMES,
  isAppearance,
  type AppearanceDto,
  type ColorScheme,
  type DesktopBridge,
} from "../../shared/bridge.js";

/**
 * The header's colour scheme toggle (#226). System follows the configured
 * `[ui] color_scheme`, which itself defaults to the OS; Light and Dark are
 * saved by the main process and win over it. The main process applies the
 * scheme to the native theme, which the stylesheet's colours follow; the
 * renderer marks the document root with it as well.
 */

export const COLOR_SCHEME_LABELS: Readonly<Record<ColorScheme, string>> =
  Object.freeze({ system: "System", light: "Light", dark: "Dark" });
export const COLOR_SCHEME_SAVE_FAILED = "The colour scheme could not be saved.";

type AppearanceBridge = Pick<
  DesktopBridge,
  "getAppearance" | "setColorScheme" | "onAppearance"
>;

/** Marks `root` with the scheme in effect, for the stylesheet's `color-scheme`. */
export function applyAppearance(
  root: HTMLElement,
  appearance: AppearanceDto,
): void {
  root.dataset.colorScheme = appearance.effective;
}

/** The toggle's pressed choice: the saved scheme, or System when none is. */
export function selectedColorScheme(appearance: AppearanceDto): ColorScheme {
  return appearance.saved ?? "system";
}

/** Says what System means now, which config.toml can change. */
export function colorSchemeTitle(
  scheme: ColorScheme,
  appearance: AppearanceDto | null,
): string {
  if (scheme !== "system") {
    return `Always use the ${COLOR_SCHEME_LABELS[scheme].toLowerCase()} colour scheme`;
  }
  if (appearance === null || appearance.configured === "system") {
    return "Follow the system colour scheme";
  }
  return `Use the config.toml colour scheme (${COLOR_SCHEME_LABELS[appearance.configured].toLowerCase()})`;
}

/** Keeps the newer of two appearances; a malformed one is ignored. */
export function newerAppearance(
  current: AppearanceDto | null,
  value: unknown,
): AppearanceDto | null {
  if (!isAppearance(value)) return current;
  return current !== null && current.revision >= value.revision
    ? current
    : value;
}

export function ColorSchemeToggle({
  bridge,
  root,
}: {
  readonly bridge: AppearanceBridge;
  readonly root?: HTMLElement;
}): ReactNode {
  const [appearance, setAppearance] = useState<AppearanceDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    const accept = (value: unknown): void => {
      if (active) setAppearance((current) => newerAppearance(current, value));
    };
    const unsubscribe = bridge.onAppearance(accept);
    bridge.getAppearance().then(accept, () => undefined);
    return () => {
      active = false;
      unsubscribe();
    };
  }, [bridge]);
  useEffect(() => {
    if (appearance !== null) {
      applyAppearance(root ?? document.documentElement, appearance);
    }
  }, [appearance, root]);
  const choose = useCallback(
    async (scheme: ColorScheme): Promise<void> => {
      setBusy(true);
      setFailed(false);
      try {
        const next = await bridge.setColorScheme(scheme);
        setAppearance((current) => newerAppearance(current, next));
      } catch {
        setFailed(true);
      } finally {
        setBusy(false);
      }
    },
    [bridge],
  );
  const selected = appearance === null ? null : selectedColorScheme(appearance);
  return (
    <div
      className="color-scheme-toggle"
      role="group"
      aria-label="Colour scheme"
    >
      {COLOR_SCHEMES.map((scheme) => (
        <button
          key={scheme}
          type="button"
          className={`button ${selected === scheme ? "button-active" : "button-secondary"}`}
          aria-pressed={selected === scheme}
          data-color-scheme-choice={scheme}
          disabled={busy}
          title={colorSchemeTitle(scheme, appearance)}
          onClick={() => void choose(scheme)}
        >
          {COLOR_SCHEME_LABELS[scheme]}
        </button>
      ))}
      {failed && (
        <span className="color-scheme-error" role="status">
          {COLOR_SCHEME_SAVE_FAILED}
        </span>
      )}
    </div>
  );
}
