// webui/src/tests/mermaid-preview.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: "<svg id='m' viewBox='0 0 800 200'></svg>" }),
  },
}));

import MermaidPreview from "@/components/rich/MermaidPreview";

describe("MermaidPreview", () => {
  it("renders the produced SVG", async () => {
    const { container } = render(<MermaidPreview code="graph TD; A-->B" />);
    await waitFor(() =>
      expect(container.querySelector("svg")).not.toBeNull(),
    );
  });

  it("shows an error when rendering throws", async () => {
    const mermaid = (await import("mermaid")).default;
    (mermaid.render as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("bad"),
    );
    render(<MermaidPreview code="not a diagram" />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });

  it("renders the SVG at its intrinsic width so the container controls fit", async () => {
    const { container } = render(<MermaidPreview code="graph TD; A-->B" />);
    await waitFor(() => {
      const svg = container.querySelector("svg") as SVGSVGElement;
      expect(svg).not.toBeNull();
      expect(svg.style.width).toBe("800px");
      expect(svg.style.maxWidth).toBe("none");
    });
  });

  it("reports the rendered SVG via onRendered", async () => {
    const onRendered = vi.fn();
    render(<MermaidPreview code="graph TD; A-->B" onRendered={onRendered} />);
    await waitFor(() => expect(onRendered).toHaveBeenCalledWith(expect.stringContaining("<svg")));
  });
});
