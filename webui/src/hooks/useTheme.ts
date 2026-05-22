import { useCallback, useEffect, useState } from "react";

type Theme = "light" | "dark";

/** durin's colour palettes — see design/DESIGN.md. `ithildin` is the
 *  default and maps to `:root` (no attribute needed). */
export type Palette = "ithildin" | "forge" | "mithril";
export const PALETTES: Palette[] = ["ithildin", "forge", "mithril"];

const STORAGE_KEY = "durin-webui.theme";
const PALETTE_KEY = "durin-webui.palette";

function readStored(): Theme | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

function readStoredPalette(): Palette {
  try {
    const v = localStorage.getItem(PALETTE_KEY);
    if (v === "forge" || v === "mithril" || v === "ithildin") return v;
  } catch {
    // ignore
  }
  return "ithildin";
}

function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "dark") root.classList.add("dark");
  else root.classList.remove("dark");
}

function applyPalette(palette: Palette): void {
  const root = document.documentElement;
  // Ithildin is `:root` in globals.css — no attribute means default.
  if (palette === "ithildin") root.removeAttribute("data-palette");
  else root.setAttribute("data-palette", palette);
}

export function useTheme(): {
  theme: Theme;
  toggle: () => void;
  setTheme: (t: Theme) => void;
  palette: Palette;
  setPalette: (p: Palette) => void;
} {
  const [theme, setThemeState] = useState<Theme>(() => {
    const stored = readStored();
    if (stored) return stored;
    if (typeof window !== "undefined" && window.matchMedia) {
      return window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    }
    return "light";
  });
  const [palette, setPaletteState] = useState<Palette>(readStoredPalette);

  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // ignore
    }
  }, [theme]);

  useEffect(() => {
    applyPalette(palette);
    try {
      localStorage.setItem(PALETTE_KEY, palette);
    } catch {
      // ignore
    }
  }, [palette]);

  const setTheme = useCallback((t: Theme) => setThemeState(t), []);
  const toggle = useCallback(
    () => setThemeState((t) => (t === "dark" ? "light" : "dark")),
    [],
  );
  const setPalette = useCallback((p: Palette) => setPaletteState(p), []);
  return { theme, toggle, setTheme, palette, setPalette };
}
