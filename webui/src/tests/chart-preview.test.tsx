// webui/src/tests/chart-preview.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const embed = vi.fn().mockResolvedValue({ finalize: vi.fn() });
vi.mock("vega-embed", () => ({ default: embed }));

import ChartPreview from "@/components/rich/ChartPreview";

beforeEach(() => {
  embed.mockClear();
});

describe("ChartPreview", () => {
  it("embeds a parsed Vega-Lite spec", async () => {
    const spec = '{"mark":"bar","data":{"values":[{"a":1}]}}';
    render(<ChartPreview code={spec} />);
    await waitFor(() => expect(embed).toHaveBeenCalledTimes(1));
    expect(embed.mock.calls[0][1]).toMatchObject({ mark: "bar" });
  });

  it("shows an error for invalid JSON", async () => {
    render(<ChartPreview code="{not json" />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });

  it("rejects specs with a remote url and does not call embed", async () => {
    const remoteSpec = '{"mark":"bar","data":{"url":"https://example.com/data.json"}}';
    render(<ChartPreview code={remoteSpec} />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(embed).not.toHaveBeenCalled();
  });
});
