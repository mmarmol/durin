// webui/src/tests/mermaid-cache.test.tsx
import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// Mock the lazily-imported mermaid module so we can count render calls and
// drive output deterministically.
const renderMock = vi.fn(async (_id: string, code: string) => ({
  svg: `<svg data-code="${code}">diagram</svg>`,
}));
vi.mock("mermaid", () => ({
  default: { initialize: vi.fn(), render: renderMock },
}));

import MermaidPreview from "@/components/rich/MermaidPreview";

afterEach(() => {
  renderMock.mockClear();
});

describe("MermaidPreview content cache", () => {
  it("renders the diagram from mermaid on first mount", async () => {
    const { container } = render(<MermaidPreview code="graph TD; A-->B" />);
    await waitFor(() =>
      expect(container.querySelector("svg")).not.toBeNull(),
    );
    expect(renderMock).toHaveBeenCalledTimes(1);
  });

  it("on remount with the same code, paints immediately without a blank flash and without re-rendering", async () => {
    const code = "graph TD; X-->Y";
    const first = render(<MermaidPreview code={code} />);
    await waitFor(() =>
      expect(first.container.querySelector("svg")).not.toBeNull(),
    );
    expect(renderMock).toHaveBeenCalledTimes(1);
    first.unmount();
    renderMock.mockClear();

    // Remount with identical source (what the transcript does on a poll/stream
    // update). The cached SVG must be present on the very first paint — no "…"
    // placeholder — and mermaid.render must not run again.
    const second = render(<MermaidPreview code={code} />);
    expect(second.container.querySelector("svg")).not.toBeNull();
    expect(second.container.textContent).not.toContain("…");
    expect(renderMock).not.toHaveBeenCalled();
  });

  it("renders fresh (no cache hit) for a previously unseen diagram", async () => {
    const second = render(<MermaidPreview code="graph TD; NEW-->NODE" />);
    // Before the async render resolves, the loading placeholder shows.
    expect(second.container.textContent).toContain("…");
    await waitFor(() =>
      expect(second.container.querySelector("svg")).not.toBeNull(),
    );
    expect(renderMock).toHaveBeenCalledTimes(1);
  });
});
