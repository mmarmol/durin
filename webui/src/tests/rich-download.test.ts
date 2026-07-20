import { afterEach, describe, expect, it, vi } from "vitest";
import { downloadBlob } from "@/components/rich/download";

afterEach(() => vi.restoreAllMocks());

describe("download", () => {
  it("creates a blob URL and clicks an anchor with the given filename", () => {
    const createUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    downloadBlob("diagram.svg", "image/svg+xml", "<svg/>");

    const blob = createUrl.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("image/svg+xml");
    expect(click).toHaveBeenCalledOnce();
    expect(revoke).toHaveBeenCalledWith("blob:x");
  });
});
