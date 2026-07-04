import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ThreadComposer } from "@/components/thread/ThreadComposer";
import type { EncodeResponse } from "@/lib/imageEncode";

// The composer imports imageEncode for the image pipeline; documents must NOT
// touch it. Stub it so we can assert it is never called for a document.
const encodeImage = vi.fn<(file: File) => Promise<EncodeResponse>>();

vi.mock("@/lib/imageEncode", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/imageEncode")>();
  return {
    ...actual,
    encodeImage: (file: File) => encodeImage(file),
  };
});

function docFile(name = "spec.pdf", type = "application/pdf", size = 12): File {
  return new File([new Uint8Array(size)], name, { type });
}

function fileInputOf(): HTMLInputElement {
  return screen
    .getByLabelText(/message input/i)
    .closest("form")!
    .querySelector('input[type="file"]') as HTMLInputElement;
}

/** Wait until every attached document finished reading (its data URL is
 * built). The chip renders at the ``reading`` stage — one microtask before the
 * data URL resolves — so gating on the chip alone races the async read; the
 * "Reading…" spinner label clearing means every document is ``ready``. */
async function waitForDocsReady(expectedChips: number): Promise<void> {
  await waitFor(() => {
    expect(screen.getAllByTestId("document-chip")).toHaveLength(expectedChips);
    expect(screen.queryByLabelText("Reading…")).toBeNull();
  });
}

beforeEach(() => {
  encodeImage.mockReset();
  let id = 0;
  if (!(globalThis.URL as unknown as { createObjectURL?: unknown }).createObjectURL) {
    (globalThis.URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL =
      () => `blob:mock/${++id}`;
  }
  if (!(globalThis.URL as unknown as { revokeObjectURL?: unknown }).revokeObjectURL) {
    (globalThis.URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL =
      () => {};
  }
});

describe("ThreadComposer — document attachments", () => {
  it("attaches a .pdf and includes it on the wire media as a document data URL", async () => {
    const onSend = vi.fn();
    render(<ThreadComposer onSend={onSend} />);

    await act(async () => {
      fireEvent.change(fileInputOf(), { target: { files: [docFile("spec.pdf")] } });
    });

    await waitForDocsReady(1);
    // Filename is shown on the chip.
    expect(screen.getByTestId("document-chip").textContent ?? "").toContain(
      "spec.pdf",
    );
    // Documents are never routed through the image encoder.
    expect(encodeImage).not.toHaveBeenCalled();

    const textarea = screen.getByLabelText(/message input/i);
    fireEvent.change(textarea, { target: { value: "please read this" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onSend).toHaveBeenCalledTimes(1);
    const [content, payload] = onSend.mock.calls[0];
    expect(content).toBe("please read this");
    expect(payload).toHaveLength(1);
    expect(payload[0].media.data_url).toMatch(/^data:application\/pdf;base64,/);
    expect(payload[0].media.name).toBe("spec.pdf");
    // Document previews are filename chips, not image thumbnails.
    expect(payload[0].previewFile).toEqual({ kind: "file", name: "spec.pdf" });
    expect(payload[0].preview).toBeUndefined();
  });

  it("derives a document MIME from the extension when the browser reports a blank type", async () => {
    const onSend = vi.fn();
    render(<ThreadComposer onSend={onSend} />);

    await act(async () => {
      fireEvent.change(fileInputOf(), {
        target: { files: [docFile("notes.md", "")] },
      });
    });

    await waitForDocsReady(1);

    const textarea = screen.getByLabelText(/message input/i);
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onSend).toHaveBeenCalledTimes(1);
    const payload = onSend.mock.calls[0][1];
    expect(payload[0].media.data_url).toMatch(/^data:text\/markdown;base64,/);
    expect(payload[0].media.name).toBe("notes.md");
  });

  it("can send a document with no text", async () => {
    const onSend = vi.fn();
    render(<ThreadComposer onSend={onSend} />);

    await act(async () => {
      fireEvent.change(fileInputOf(), { target: { files: [docFile("a.pdf")] } });
    });
    await waitForDocsReady(1);

    const textarea = screen.getByLabelText(/message input/i);
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0]).toBe("");
    expect(onSend.mock.calls[0][1]).toHaveLength(1);
  });

  it("rejects an oversized document (26 MB) with an inline error and no chip", async () => {
    const onSend = vi.fn();
    render(<ThreadComposer onSend={onSend} />);

    const big = docFile("big.pdf");
    Object.defineProperty(big, "size", { value: 26 * 1024 * 1024 });

    await act(async () => {
      fireEvent.change(fileInputOf(), { target: { files: [big] } });
    });

    expect(screen.queryByTestId("document-chip")).toBeNull();
    expect(await screen.findByRole("alert")).toHaveTextContent(/25 MB|too large/i);
  });

  it("rejects a 4th document beyond the per-message cap", async () => {
    const onSend = vi.fn();
    render(<ThreadComposer onSend={onSend} />);

    await act(async () => {
      fireEvent.change(fileInputOf(), {
        target: {
          files: [docFile("a.pdf"), docFile("b.pdf"), docFile("c.pdf")],
        },
      });
    });
    await waitFor(() =>
      expect(screen.getAllByTestId("document-chip")).toHaveLength(3),
    );

    await act(async () => {
      fireEvent.change(fileInputOf(), { target: { files: [docFile("d.pdf")] } });
    });

    // Still only 3 chips; the 4th is rejected with an inline error.
    expect(screen.getAllByTestId("document-chip")).toHaveLength(3);
    expect(await screen.findByRole("alert")).toHaveTextContent(/max 3|documents/i);
  });

  it("keeps images working alongside documents on the same send", async () => {
    const onSend = vi.fn();
    encodeImage.mockResolvedValueOnce({
      id: "stub",
      ok: true,
      dataUrl: "data:image/png;base64,QUJD",
      mime: "image/png",
      bytes: 3,
      origBytes: 3,
      normalized: false,
    } as EncodeResponse);

    render(<ThreadComposer onSend={onSend} />);

    await act(async () => {
      fireEvent.change(fileInputOf(), {
        target: {
          files: [
            new File([new Uint8Array(3)], "shot.png", { type: "image/png" }),
            docFile("spec.pdf"),
          ],
        },
      });
    });

    await waitFor(() =>
      expect(screen.getByTestId("composer-chip")).toBeInTheDocument(),
    );
    await waitForDocsReady(1);

    const textarea = screen.getByLabelText(/message input/i);
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onSend).toHaveBeenCalledTimes(1);
    const payload = onSend.mock.calls[0][1];
    expect(payload).toHaveLength(2);
    const kinds = payload.map((p: { media: { data_url: string } }) =>
      p.media.data_url.slice(0, p.media.data_url.indexOf(";")),
    );
    expect(kinds).toContain("data:image/png");
    expect(kinds).toContain("data:application/pdf");
  });
});
