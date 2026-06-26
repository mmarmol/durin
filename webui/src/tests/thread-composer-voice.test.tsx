import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ThreadComposer } from "@/components/thread/ThreadComposer";

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ token: "t" }),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, fetchModelPicker: vi.fn().mockResolvedValue([]) };
});

describe("ThreadComposer — voice orb entry", () => {
  it("renders a voice orb that enters voice mode on click", () => {
    const onEnterVoice = vi.fn();
    render(
      <ThreadComposer onSend={vi.fn()} onEnterVoice={onEnterVoice} placeholder="Type a message..." />,
    );
    const orb = screen.getByRole("button", { name: /start voice/i });
    fireEvent.click(orb);
    expect(onEnterVoice).toHaveBeenCalledTimes(1);
  });

  it("hides the voice orb when voice is unavailable (no onEnterVoice)", () => {
    render(<ThreadComposer onSend={vi.fn()} placeholder="Type a message..." />);
    expect(screen.queryByRole("button", { name: /start voice/i })).toBeNull();
  });
});
