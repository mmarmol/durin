import { describe, expect, it, vi } from "vitest";

import { readCanvasTheme, watchTheme } from "@/lib/canvas-theme";

describe("canvas theme", () => {
  it("returns a non-empty color for every slot", () => {
    const t = readCanvasTheme();
    for (const v of Object.values(t)) {
      expect(typeof v).toBe("string");
      expect(v.length).toBeGreaterThan(0);
    }
  });

  it("notifies on root class changes and disposes cleanly", async () => {
    const cb = vi.fn();
    const stop = watchTheme(cb);
    document.documentElement.classList.add("dark");
    await new Promise((r) => setTimeout(r, 0));
    stop();
    document.documentElement.classList.remove("dark");
    await new Promise((r) => setTimeout(r, 0));
    expect(cb).toHaveBeenCalledTimes(1);
  });
});
