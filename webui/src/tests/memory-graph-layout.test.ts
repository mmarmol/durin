import { describe, expect, it } from "vitest";

import {
  labelCellSize,
  NODE_RADIUS_MAX,
  radiusForBubble,
  radiusForNode,
  SESSION_RADIUS,
  visibleLabels,
} from "@/lib/memory-graph-layout";

describe("radiusForNode", () => {
  it("caps growth for mega-weights", () => {
    expect(radiusForNode(100_000, "person")).toBeLessThanOrEqual(NODE_RADIUS_MAX);
  });
  it("sessions are fixed-size regardless of message count", () => {
    expect(radiusForNode(4, "session")).toBe(SESSION_RADIUS);
    expect(radiusForNode(4000, "session")).toBe(SESSION_RADIUS);
  });
  it("is monotonic for entities", () => {
    expect(radiusForNode(50, "topic")).toBeGreaterThan(radiusForNode(2, "topic"));
  });
});

describe("radiusForBubble", () => {
  it("stays within bounds", () => {
    expect(radiusForBubble(1)).toBeGreaterThanOrEqual(18);
    expect(radiusForBubble(100_000)).toBeLessThanOrEqual(60);
  });
});

describe("labelCellSize", () => {
  it("shrinks as zoom increases (more, smaller cells zoomed in)", () => {
    expect(labelCellSize(0.5)).toBeGreaterThan(labelCellSize(1.2));
    expect(labelCellSize(1.2)).toBeGreaterThan(labelCellSize(3));
  });
});

describe("visibleLabels", () => {
  const vp = { w: 800, h: 600 };
  it("admits one winner per grid cell — the heavier of two colliding candidates", () => {
    const cands = [
      { id: "a", sx: 100, sy: 100, weight: 9 },
      { id: "b", sx: 104, sy: 102, weight: 8 },
    ];
    const out = visibleLabels(cands, vp, 90);
    expect(out.has("a")).toBe(true);
    expect(out.has("b")).toBe(false);
  });
  it("a priority candidate claims its cell over a heavier non-priority candidate", () => {
    // Ordering sorts priority first regardless of weight, so the lighter
    // priority candidate is placed — and claims the shared cell — before the
    // heavier one is even considered.
    const cands = [
      { id: "hover", sx: 100, sy: 100, weight: 0, priority: true },
      { id: "big", sx: 104, sy: 102, weight: 100 },
    ];
    const out = visibleLabels(cands, vp, 90);
    expect(out.has("hover")).toBe(true);
    expect(out.has("big")).toBe(false);
  });
  it("labels every candidate when each sits in its own cell (no numeric cap)", () => {
    // 8 columns x 7 rows spaced 100px apart, well past the 90px cell size —
    // no two candidates share a grid cell, so all 50 must be labeled.
    const cands = Array.from({ length: 50 }, (_, i) => ({
      id: `n${i}`, sx: (i % 8) * 100, sy: Math.floor(i / 8) * 100, weight: i,
    }));
    const out = visibleLabels(cands, vp, 90);
    expect(out.size).toBe(50);
  });
  it("drops off-viewport candidates", () => {
    const out = visibleLabels([{ id: "far", sx: 4000, sy: 4000, weight: 50 }], vp, 90);
    expect(out.size).toBe(0);
  });
});
