import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DocumentsShelf } from "@/components/DocumentsShelf";
import * as api from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchMemoryDocuments: vi.fn(),
    fetchMemoryDocument: vi.fn(),
    forgetMemoryDocument: vi.fn(),
  };
});

const SUMMARY: api.ReferenceDocumentSummary = {
  slug: "the-durin-handbook",
  ref: "reference:the-durin-handbook",
  title: "The Durin Handbook",
  source: null,
  ingested_at: "2026-07-16T00:00:00Z",
  chunk_count: 3,
  distilled: true,
};

const DETAIL: api.ReferenceDocumentDetail = {
  slug: "the-durin-handbook",
  ref: "reference:the-durin-handbook",
  title: "The Durin Handbook",
  source: null,
  ingested_at: "2026-07-16T00:00:00Z",
  chunk_count: 3,
  chunks_total: 3,
  body: "<!-- provenance: hidden -->\n# Intro\n\nDurin is a **local** agent.\n",
  outline: {
    abstract: "A handbook about the durin local agent.",
    sections: [{ breadcrumb: "Intro", summary: "What durin is.", chunk_indices: [0] }],
  },
  entities: [],
  chunks_preview: [
    { idx: 0, breadcrumb: "Intro", text: "Durin is a **local** agent." },
    { idx: 1, breadcrumb: "Intro › Setup", text: "Run the gateway." },
  ],
};

beforeEach(() => {
  vi.mocked(api.fetchMemoryDocuments).mockReset().mockResolvedValue([SUMMARY]);
  vi.mocked(api.fetchMemoryDocument).mockReset().mockResolvedValue(DETAIL);
});

async function openHandbook() {
  render(<DocumentsShelf token="tok" active />);
  await userEvent.click(await screen.findByText("The Durin Handbook"));
  await waitFor(() => expect(api.fetchMemoryDocument).toHaveBeenCalled());
}

describe("DocumentsShelf reading view", () => {
  it("renders the full body as markdown on the default Document tab", async () => {
    await openHandbook();
    // Markdown actually rendered: heading element + inline emphasis, not
    // literal ``#`` / ``**`` source text.
    const heading = await screen.findByRole("heading", { name: "Intro" });
    expect(heading.tagName).toBe("H1");
    const bold = screen.getByText("local");
    expect(bold.tagName).toBe("STRONG");
    // Provenance HTML comments are stripped before rendering.
    expect(screen.queryByText(/provenance: hidden/)).toBeNull();
    // Distilled header sits above the body.
    expect(
      screen.getByText("A handbook about the durin local agent."),
    ).toBeInTheDocument();
  });

  it("shows the indexed chunks verbatim on the Fragments tab", async () => {
    await openHandbook();
    await userEvent.click(screen.getByRole("button", { name: /Fragments/ }));
    // Fragments are the raw indexed text — markdown stays literal.
    expect(
      await screen.findByText("Durin is a **local** agent."),
    ).toBeInTheDocument();
    expect(screen.getByText("Intro › Setup")).toBeInTheDocument();
    // Bounded preview is labelled as such (2 of 3 chunks shown).
    expect(screen.getByText("Showing 2 of 3 chunks.")).toBeInTheDocument();
  });
});
