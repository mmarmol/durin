import { render, screen, act } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { useConcurrencySnapshot } from "./useConcurrencySnapshot";

const handlers = new Set<(ev: unknown) => void>();
const fakeClient = {
  onConcurrencySnapshot: (h: (ev: unknown) => void) => {
    handlers.add(h);
    return () => handlers.delete(h);
  },
};

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ client: fakeClient, token: "t" }),
}));

function Probe() {
  const snap = useConcurrencySnapshot();
  return <div>{snap ? `${snap.lanes.ceiling.active}/${snap.lanes.ceiling.limit}` : "none"}</div>;
}

describe("useConcurrencySnapshot", () => {
  it("starts null then updates on event", () => {
    render(<Probe />);
    expect(screen.getByText("none")).toBeInTheDocument();
    act(() => {
      for (const h of handlers)
        h({
          event: "concurrency_snapshot",
          lanes: {
            interactive: { active: 1, limit: 4, waiting: 0 },
            ceiling: { active: 5, limit: 12, waiting: 0 },
            subagents: { active: 2, limit: 3 },
          },
          queued: 0,
          work: [],
        });
    });
    expect(screen.getByText("5/12")).toBeInTheDocument();
  });
});
