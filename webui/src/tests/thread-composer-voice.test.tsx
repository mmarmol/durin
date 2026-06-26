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

  it("shows the active-call strip with the live state; the orb is the only stop", () => {
    render(
      <ThreadComposer
        onSend={vi.fn()}
        onEnterVoice={vi.fn()}
        voiceActive
        voiceState="listening"
        placeholder="Type a message..."
      />,
    );
    // The strip reflects the state and carries no stop button of its own.
    expect(screen.getByRole("status")).toHaveTextContent(/listening/i);
    // Ending the call is the orb (its label flips to stop while active).
    expect(screen.getByRole("button", { name: /stop voice/i })).toBeInTheDocument();
  });
});
