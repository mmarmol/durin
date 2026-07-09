import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MessageBubble } from "@/components/MessageBubble";
import { inferMediaKind } from "@/lib/media";
import type { UIMessage } from "@/lib/types";

function assistantWithAttachment(url: string, name: string): UIMessage {
  return {
    id: "a-html",
    role: "assistant",
    content: "Here is the mockup:",
    createdAt: Date.now(),
    media: [{ kind: "html", url, name }],
  };
}

describe("html attachments", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("infers the html kind from the file extension", () => {
    expect(inferMediaKind({ url: "/api/media/sig/mockup.html" })).toBe("html");
    expect(inferMediaKind({ name: "page.htm" })).toBe("html");
    expect(inferMediaKind({ url: "/api/media/sig/mockup.html?tok=1" })).toBe("html");
    expect(inferMediaKind({ name: "notes.pdf" })).toBe("file");
    expect(inferMediaKind({ name: "photo.png" })).toBe("image");
  });

  it("renders an .html attachment in the sandboxed preview with a download caption", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        text: () => Promise.resolve("<h1>Andino</h1>"),
      }),
    );

    render(
      <MessageBubble
        message={assistantWithAttachment("/api/media/sig/andino.html", "andino.html")}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTitle("rich-html")).toBeInTheDocument();
    });
    const download = screen.getByRole("link", { name: "andino.html" });
    expect(download).toHaveAttribute("href", "/api/media/sig/andino.html");
    expect(download).toHaveAttribute("download", "andino.html");
  });

  it("falls back to the file chip when the fetch fails", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("boom"));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MessageBubble
        message={assistantWithAttachment("/api/media/sig/broken.html", "broken.html")}
      />,
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    expect(screen.queryByTitle("rich-html")).not.toBeInTheDocument();
    expect(screen.getByText("broken.html")).toBeInTheDocument();
  });
});
