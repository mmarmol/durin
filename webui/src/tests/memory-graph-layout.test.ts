import { describe, expect, it } from "vitest";

import {
  labelBudget,
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

describe("labelBudget", () => {
  it("grows with zoom", () => {
    expect(labelBudget(0.5)).toBeLessThan(labelBudget(1.2));
    expect(labelBudget(1.2)).toBeLessThan(labelBudget(3));
  });
});

describe("visibleLabels", () => {
  const vp = { w: 800, h: 600 };
  it("respects the budget, highest weight first", () => {
    const cands = Array.from({ length: 50 }, (_, i) => ({
      id: `n${i}`, sx: (i % 10) * 80, sy: Math.floor(i / 10) * 110, weight: i,
    }));
    const out = visibleLabels(cands, vp, 5);
    expect(out.size).toBeLessThanOrEqual(5);
    expect(out.has("n49")).toBe(true);
  });
  it("never places two labels in the same grid cell", () => {
    const cands = [
      { id: "a", sx: 100, sy: 100, weight: 9 },
      { id: "b", sx: 104, sy: 102, weight: 8 },
    ];
    const out = visibleLabels(cands, vp, 10, 90);
    expect(out.has("a")).toBe(true);
    expect(out.has("b")).toBe(false);
  });
  it("priority candidates always win, even over the budget", () => {
    const cands = [
      { id: "hover", sx: 10, sy: 10, weight: 0, priority: true },
      { id: "big", sx: 400, sy: 400, weight: 100 },
    ];
    const out = visibleLabels(cands, vp, 1);
    expect(out.has("hover")).toBe(true);
  });
  it("drops off-viewport candidates", () => {
    const out = visibleLabels([{ id: "far", sx: 4000, sy: 4000, weight: 50 }], vp, 10);
    expect(out.size).toBe(0);
  });
});
