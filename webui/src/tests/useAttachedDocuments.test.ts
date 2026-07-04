import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  MAX_DOCUMENT_BYTES,
  isDocumentFile,
  resolveDocumentMime,
  useAttachedDocuments,
} from "@/hooks/useAttachedDocuments";

function file(name: string, type = "", size = 8): File {
  return new File([new Uint8Array(size)], name, { type });
}

describe("resolveDocumentMime", () => {
  it("prefers the extension for a canonical wire MIME", () => {
    expect(resolveDocumentMime(file("a.pdf", "application/pdf"))).toBe(
      "application/pdf",
    );
    // Browser reported blank type (common for .md / .epub) → derive from ext.
    expect(resolveDocumentMime(file("notes.md", ""))).toBe("text/markdown");
    expect(resolveDocumentMime(file("book.epub", ""))).toBe(
      "application/epub+zip",
    );
    // text/xml normalizes to the canonical application/xml.
    expect(resolveDocumentMime(file("data.xml", "text/xml"))).toBe(
      "application/xml",
    );
  });

  it("falls back to a whitelisted reported MIME when the extension is unknown", () => {
    // No mapped extension, but the reported MIME is on the whitelist.
    expect(resolveDocumentMime(file("weird", "application/pdf"))).toBe(
      "application/pdf",
    );
  });

  it("rejects non-document files", () => {
    expect(resolveDocumentMime(file("a.png", "image/png"))).toBeNull();
    expect(resolveDocumentMime(file("a.mp3", "audio/mpeg"))).toBeNull();
    expect(resolveDocumentMime(file("noext", ""))).toBeNull();
    expect(isDocumentFile(file("a.png", "image/png"))).toBe(false);
    expect(isDocumentFile(file("a.pdf", ""))).toBe(true);
  });
});

describe("useAttachedDocuments", () => {
  it("enqueues a .pdf and resolves it to a base64 data URL with the doc MIME", async () => {
    const { result } = renderHook(() => useAttachedDocuments());

    act(() => {
      const { rejected } = result.current.enqueue([file("doc.pdf", "application/pdf")]);
      expect(rejected).toHaveLength(0);
    });

    // Optimistic: chip present, still reading.
    expect(result.current.documents).toHaveLength(1);
    expect(result.current.documents[0].name).toBe("doc.pdf");

    await waitFor(() =>
      expect(result.current.documents[0].status).toBe("ready"),
    );
    const dataUrl = result.current.documents[0].dataUrl ?? "";
    expect(dataUrl.startsWith("data:application/pdf;base64,")).toBe(true);
  });

  it("resolves a blank-type .md to text/markdown on the data URL", async () => {
    const { result } = renderHook(() => useAttachedDocuments());
    act(() => {
      result.current.enqueue([file("readme.md", "")]);
    });
    await waitFor(() =>
      expect(result.current.documents[0].status).toBe("ready"),
    );
    expect(result.current.documents[0].dataUrl ?? "").toContain(
      "data:text/markdown;base64,",
    );
  });

  it("rejects an oversized document without adding a chip", () => {
    const { result } = renderHook(() => useAttachedDocuments());
    let rejected: Array<{ reason: string }> = [];
    act(() => {
      // File constructor won't allocate 26 MB; stub .size instead.
      const big = file("big.pdf", "application/pdf");
      Object.defineProperty(big, "size", { value: MAX_DOCUMENT_BYTES + 1 });
      rejected = result.current.enqueue([big]).rejected;
    });
    expect(rejected).toHaveLength(1);
    expect(rejected[0].reason).toBe("too_large");
    expect(result.current.documents).toHaveLength(0);
  });

  it("rejects a 4th document beyond the per-message cap", () => {
    const { result } = renderHook(() => useAttachedDocuments());
    act(() => {
      result.current.enqueue([
        file("a.pdf", "application/pdf"),
        file("b.pdf", "application/pdf"),
        file("c.pdf", "application/pdf"),
      ]);
    });
    expect(result.current.documents).toHaveLength(3);
    expect(result.current.full).toBe(true);

    let rejected: Array<{ reason: string }> = [];
    act(() => {
      rejected = result.current.enqueue([file("d.pdf", "application/pdf")]).rejected;
    });
    expect(rejected).toHaveLength(1);
    expect(rejected[0].reason).toBe("too_many");
    expect(result.current.documents).toHaveLength(3);
  });

  it("rejects an unsupported type", () => {
    const { result } = renderHook(() => useAttachedDocuments());
    let rejected: Array<{ reason: string }> = [];
    act(() => {
      rejected = result.current.enqueue([file("a.png", "image/png")]).rejected;
    });
    expect(rejected).toHaveLength(1);
    expect(rejected[0].reason).toBe("unsupported_type");
    expect(result.current.documents).toHaveLength(0);
  });

  it("removes and clears attachments", async () => {
    const { result } = renderHook(() => useAttachedDocuments());
    act(() => {
      result.current.enqueue([
        file("a.pdf", "application/pdf"),
        file("b.pdf", "application/pdf"),
      ]);
    });
    await waitFor(() =>
      expect(
        result.current.documents.every((d) => d.status === "ready"),
      ).toBe(true),
    );
    const firstId = result.current.documents[0].id;
    act(() => result.current.remove(firstId));
    expect(result.current.documents).toHaveLength(1);
    act(() => result.current.clear());
    expect(result.current.documents).toHaveLength(0);
  });
});
