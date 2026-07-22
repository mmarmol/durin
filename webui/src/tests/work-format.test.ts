import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { activeNode, formatElapsed, useTicker } from "@/lib/work-format";

describe("formatElapsed", () => {
  it("renders minutes and seconds zero-padded", () => {
    expect(formatElapsed(1000, 1000 + 261_000)).toBe("4:21");
  });
  it("renders hours past sixty minutes", () => {
    expect(formatElapsed(0, 3861_000)).toBe("1:04:21");
  });
  it("clamps a clock that would run backwards", () => {
    expect(formatElapsed(5000, 1000)).toBe("0:00");
  });
});

describe("activeNode", () => {
  it("returns the running node", () => {
    const item = { kind: "workflow", id: "r", label: "wf", status: "running",
      startedAt: 0, endedAt: null,
      nodes: [{ id: "a", status: "done" }, { id: "b", status: "running" }] } as never;
    expect(activeNode(item)?.id).toBe("b");
  });
  it("returns undefined when nothing is running", () => {
    const item = { kind: "workflow", id: "r", label: "wf", status: "done",
      startedAt: 0, endedAt: null, nodes: [{ id: "a", status: "done" }] } as never;
    expect(activeNode(item)).toBeUndefined();
  });
});

describe("useTicker", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not tick while inactive", () => {
    const { result } = renderHook(() => useTicker(false));
    const first = result.current;
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe(first);
  });

  it("ticks once a second while active", () => {
    const { result } = renderHook(() => useTicker(true));
    const first = result.current;
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(first + 1000);
  });

  it("stops ticking (clears its interval) once active flips to false", () => {
    const { result, rerender } = renderHook(
      ({ active }) => useTicker(active),
      { initialProps: { active: true } },
    );
    rerender({ active: false });
    const stoppedAt = result.current;
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    // No further re-renders happened, so the returned value is stale on purpose.
    expect(result.current).toBe(stoppedAt);
  });

  it("clears its interval on unmount, leaving no pending timer", () => {
    const clearSpy = vi.spyOn(global, "clearInterval");
    const { unmount } = renderHook(() => useTicker(true));
    unmount();
    expect(clearSpy).toHaveBeenCalled();
  });
});
