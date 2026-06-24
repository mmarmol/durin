import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { DreamView } from "@/components/DreamView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchDreamDigest: vi.fn(),
  };
});

function wrap(children: ReactNode) {
  return (
    <ClientProvider
      client={{} as unknown as import("@/lib/durin-client").DurinClient}
      token="tok"
    >
      {children}
    </ClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(api.fetchDreamDigest).mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe("DreamView", () => {
  it("renders event summaries returned by fetchDreamDigest", async () => {
    const now = Date.now();
    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run_at_ms: now - 60_000,
      events: [
        { at_ms: now - 120_000, kind: "merged", ref: null, ref_kind: null, summary: "Merged entity Alpha into Beta" },
        { at_ms: now - 180_000, kind: "improved", ref: "skill:git", ref_kind: "skill", summary: "Improved the git skill" },
      ],
    });

    render(wrap(<DreamView />));

    expect(await screen.findByText("Merged entity Alpha into Beta")).toBeInTheDocument();
    expect(screen.getByText("Improved the git skill")).toBeInTheDocument();
    expect(api.fetchDreamDigest).toHaveBeenCalledWith("tok");
  });

  it("shows the empty state when there are no events", async () => {
    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run_at_ms: null,
      events: [],
    });

    render(wrap(<DreamView />));

    expect(await screen.findByText("No dream activity yet.")).toBeInTheDocument();
  });

  it("shows an error when the fetch fails", async () => {
    vi.mocked(api.fetchDreamDigest).mockRejectedValue(new Error("HTTP 500"));

    render(wrap(<DreamView />));

    expect(await screen.findByText("HTTP 500")).toBeInTheDocument();
  });
});
