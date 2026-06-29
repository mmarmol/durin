import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useTokenRefresh } from "./useTokenRefresh";

afterEach(() => vi.useRealTimers());

describe("useTokenRefresh", () => {
  it("refreshes the token before it expires, then keeps refreshing", () => {
    vi.useFakeTimers();
    const refresh = vi.fn();
    renderHook(() => useTokenRefresh(true, 300, refresh));

    // Nothing yet — the refresh fires comfortably before the 300s expiry.
    vi.advanceTimersByTime(200_000);
    expect(refresh).not.toHaveBeenCalled();

    vi.advanceTimersByTime(50_000); // past 80% of 300s = 240s
    expect(refresh).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(240_000); // next cycle
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("does not refresh when disabled", () => {
    vi.useFakeTimers();
    const refresh = vi.fn();
    renderHook(() => useTokenRefresh(false, 300, refresh));
    vi.advanceTimersByTime(10 * 60_000);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("does not refresh when the TTL is unknown (0)", () => {
    vi.useFakeTimers();
    const refresh = vi.fn();
    renderHook(() => useTokenRefresh(true, 0, refresh));
    vi.advanceTimersByTime(10 * 60_000);
    expect(refresh).not.toHaveBeenCalled();
  });
});
