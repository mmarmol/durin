import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunElapsedStrip } from "@/components/thread/ThreadComposer";
import type { ApiRetryStatus } from "@/lib/types";

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

interface RenderProps {
  startedAt: number | null;
  goalState?: undefined;
  apiStatus?: ApiRetryStatus | null;
  onDismissApiStatus?: () => void;
}

function renderStrip(props: RenderProps) {
  render(
    <RunElapsedStrip
      startedAt={props.startedAt}
      goalState={props.goalState}
      apiStatus={props.apiStatus}
      onDismissApiStatus={props.onDismissApiStatus}
    />,
  );
}

describe("RunElapsedStrip — provider-retry row", () => {
  it("renders the provider-retry status inside the run strip when a run is active", () => {
    renderStrip({
      startedAt: nowSeconds(),
      goalState: undefined,
      apiStatus: {
        kind: "retry_wait",
        attempt: 1,
        max_attempts: 7,
        delay_s: 1,
        persistent: false,
        final: false,
      },
    });
    // Matches "attempt 1 of 7" or "Retrying · attempt 1 of 7" etc.
    expect(
      screen.getByText(/intento 1 de 7|attempt 1 of 7|Retrying|Reintentando/i),
    ).toBeInTheDocument();
  });

  it("hides the retry row when no run is active (startedAt is null)", () => {
    renderStrip({
      startedAt: null,
      goalState: undefined,
      apiStatus: {
        kind: "retry_wait",
        attempt: 3,
        max_attempts: 7,
        delay_s: 0,
        persistent: false,
        final: false,
      },
    });
    expect(
      screen.queryByText(/intento|attempt|Retrying|Reintentando/i),
    ).not.toBeInTheDocument();
  });
});
