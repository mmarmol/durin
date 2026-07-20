import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("vega-embed", () => ({
  default: vi.fn(async (host: HTMLElement) => {
    host.innerHTML = "<svg id='c'></svg>";
    return { finalize: vi.fn() };
  }),
}));

import ChartPreview from "@/components/rich/ChartPreview";

describe("ChartPreview", () => {
  it("reports the embedded SVG via onRendered", async () => {
    const onRendered = vi.fn();
    render(<ChartPreview code='{"mark":"bar"}' onRendered={onRendered} />);
    await waitFor(() => expect(onRendered).toHaveBeenCalledWith(expect.stringContaining("<svg")));
  });
});
