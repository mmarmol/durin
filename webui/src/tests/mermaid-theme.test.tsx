// webui/src/tests/mermaid-theme.test.tsx
import { act, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const initializeMock = vi.fn();
const renderMock = vi.fn(async (_id: string, code: string) => ({
  svg: `<svg data-code="${code}" viewBox="0 0 100 100">diagram</svg>`,
}));
vi.mock("mermaid", () => ({
  default: { initialize: initializeMock, render: renderMock },
}));

import MermaidPreview, {
  buildMermaidThemeVariables,
} from "@/components/rich/MermaidPreview";

afterEach(() => {
  // Reset the document theme so a test that switches it can't leak into the
  // next one (the render cache is keyed on the active theme).
  document.documentElement.classList.remove("dark");
  document.documentElement.removeAttribute("data-palette");
  initializeMock.mockClear();
  renderMock.mockClear();
});

describe("MermaidPreview durin theming", () => {
  it("derives every mermaid colour from a durin token — nothing hardcoded", () => {
    const vars = buildMermaidThemeVariables((token) => `T(${token})`);
    // Representative mappings across diagram families.
    expect(vars.background).toBe("T(--background)");
    expect(vars.primaryColor).toBe("T(--secondary)");
    expect(vars.primaryBorderColor).toBe("T(--border)");
    expect(vars.lineColor).toBe("T(--muted-foreground)");
    expect(vars.clusterBkg).toBe("T(--muted)");
    expect(vars.actorBkg).toBe("T(--secondary)");
    // Robustness guarantee: no value is a literal colour — each is a token
    // lookup, so a diagram tracks whatever durin palette/mode is active.
    for (const value of Object.values(vars)) {
      expect(value).toMatch(/^T\(--[a-z-]+\)$/);
    }
  });

  it("initialises mermaid with the base theme and durin theme variables", async () => {
    render(<MermaidPreview code="graph TD; BASE-->THEME" />);
    await waitFor(() => expect(initializeMock).toHaveBeenCalled());
    const cfg = initializeMock.mock.calls.at(-1)?.[0];
    expect(cfg.theme).toBe("base");
    expect(cfg.securityLevel).toBe("strict");
    expect(cfg.themeVariables).toBeTypeOf("object");
    expect(cfg.themeVariables.background).toBeTruthy();
    expect(cfg.themeVariables.lineColor).toBeTruthy();
  });

  it("re-renders with fresh colours when the document theme changes", async () => {
    const { container } = render(<MermaidPreview code="graph TD; THEME-->SWITCH" />);
    await waitFor(() => expect(container.querySelector("svg")).not.toBeNull());
    expect(renderMock).toHaveBeenCalledTimes(1);

    // The theme hook toggles dark by adding `.dark` to <html>. The observer
    // must pick that up and re-render (a new cache key) rather than serve the
    // light-mode SVG.
    await act(async () => {
      document.documentElement.classList.add("dark");
    });
    await waitFor(() => expect(renderMock).toHaveBeenCalledTimes(2));
  });
});
