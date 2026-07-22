import { describe, expect, it } from "vitest";

import { groupTypeLegend, type TypeLegendItem } from "@/lib/memory-graph-style";

const item = (type: string, count: number): TypeLegendItem => ({
  type,
  color: "#888",
  count,
});

describe("groupTypeLegend", () => {
  it("keeps the top N and groups the tail", () => {
    const items = Array.from({ length: 12 }, (_, i) => item(`t${i}`, 100 - i));
    const { shown, tail } = groupTypeLegend(items, 8);
    expect(shown).toHaveLength(8);
    expect(tail).toHaveLength(4);
  });

  it("no tail when under the cap", () => {
    const { shown, tail } = groupTypeLegend([item("a", 1)], 8);
    expect(shown).toHaveLength(1);
    expect(tail).toHaveLength(0);
  });

  it("sorts by descending count", () => {
    const items = [item("low", 1), item("high", 50), item("mid", 10)];
    const { shown } = groupTypeLegend(items, 8);
    expect(shown.map((i) => i.type)).toEqual(["high", "mid", "low"]);
  });

  it("breaks count ties alphabetically by type", () => {
    const items = [item("zebra", 5), item("apple", 5), item("mango", 5)];
    const { shown } = groupTypeLegend(items, 8);
    expect(shown.map((i) => i.type)).toEqual(["apple", "mango", "zebra"]);
  });

  it("puts the lowest-count items in the tail, highest-first in shown", () => {
    const items = Array.from({ length: 10 }, (_, i) => item(`t${i}`, i));
    const { shown, tail } = groupTypeLegend(items, 8);
    expect(shown.map((i) => i.type)).toEqual(["t9", "t8", "t7", "t6", "t5", "t4", "t3", "t2"]);
    expect(tail.map((i) => i.type)).toEqual(["t1", "t0"]);
  });
});
