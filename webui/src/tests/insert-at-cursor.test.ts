import { describe, expect, it } from "vitest";

import { insertAtCursor } from "@/components/math/insert-at-cursor";

describe("insertAtCursor", () => {
  it("inserts at a collapsed caret", () => {
    expect(insertAtCursor("ab", 1, 1, "X")).toEqual({ next: "aXb", caret: 2 });
  });

  it("replaces a selection", () => {
    expect(insertAtCursor("abcd", 1, 3, "X")).toEqual({ next: "aXd", caret: 2 });
  });

  it("appends at the end", () => {
    expect(insertAtCursor("hi", 2, 2, "!")).toEqual({ next: "hi!", caret: 3 });
  });
});
