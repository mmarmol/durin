import { describe, expect, it } from "vitest";
import { clampScale, fitTransform, zoomToward, MIN_SCALE, MAX_SCALE } from "@/components/rich/zoom-math";

describe("zoom-math", () => {
  it("clamps scale to [MIN, MAX]", () => {
    expect(clampScale(0.01)).toBe(MIN_SCALE);
    expect(clampScale(99)).toBe(MAX_SCALE);
    expect(clampScale(1)).toBe(1);
  });

  it("fits content centered in the viewport", () => {
    const t = fitTransform(800, 200, 400, 400);
    expect(t.scale).toBeCloseTo(0.46, 2); // min(400/800,400/200)*0.92
    expect(t.tx).toBeCloseTo(16, 0);
    expect(t.ty).toBeCloseTo(154, 0);
  });

  it("returns identity for zero-sized content", () => {
    expect(fitTransform(0, 0, 400, 400)).toEqual({ scale: 1, tx: 0, ty: 0 });
  });

  it("zooms toward a point, keeping it stationary", () => {
    const t = zoomToward({ scale: 1, tx: 0, ty: 0 }, 100, 100, 2);
    expect(t.scale).toBe(2);
    expect(t.tx).toBe(-100);
    expect(t.ty).toBe(-100);
  });

  it("respects the scale clamp while zooming", () => {
    expect(zoomToward({ scale: 8, tx: 0, ty: 0 }, 0, 0, 2).scale).toBe(MAX_SCALE);
  });
});
