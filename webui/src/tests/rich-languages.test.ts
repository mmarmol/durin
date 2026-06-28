import { describe, expect, it } from "vitest";

import { richKind } from "@/components/rich/rich-languages";

describe("richKind", () => {
  it("maps known rich languages", () => {
    expect(richKind("html")).toBe("html");
    expect(richKind("svg")).toBe("svg");
    expect(richKind("mermaid")).toBe("mermaid");
    expect(richKind("vega-lite")).toBe("chart");
    expect(richKind("vega")).toBe("chart");
  });

  it("is case-insensitive", () => {
    expect(richKind("HTML")).toBe("html");
  });

  it("returns null for non-rich languages", () => {
    expect(richKind("python")).toBeNull();
    expect(richKind(undefined)).toBeNull();
  });
});
