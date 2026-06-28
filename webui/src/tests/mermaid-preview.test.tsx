// webui/src/tests/mermaid-preview.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: "<svg id='m'></svg>" }),
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
});
