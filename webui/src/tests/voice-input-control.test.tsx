import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { VoiceInputControl } from "@/components/thread/VoiceInputControl";

describe("VoiceInputControl", () => {
  it("offers dictation and hands-free behind the mic chevron when idle", () => {
    const onEnterVoice = vi.fn();
    render(<VoiceInputControl onRecorded={vi.fn()} onEnterVoice={onEnterVoice} />);

    // The hands-free orb is not a top-level button while idle.
    expect(screen.queryByRole("button", { name: /stop voice/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /voice input/i }));
    expect(screen.getByRole("menuitem", { name: /dictate to text/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: /hands-free voice/i }));
    expect(onEnterVoice).toHaveBeenCalledTimes(1);
  });

  it("collapses to a single-click stop orb during an active call", () => {
    const onEnterVoice = vi.fn();
    render(<VoiceInputControl onRecorded={vi.fn()} onEnterVoice={onEnterVoice} voiceActive />);
    const orb = screen.getByRole("button", { name: /stop voice/i });
    fireEvent.click(orb);
    expect(onEnterVoice).toHaveBeenCalledTimes(1);
  });

  it("shows the orb alone when dictation is not allowed", () => {
    render(
      <VoiceInputControl onRecorded={vi.fn()} onEnterVoice={vi.fn()} audioInputAllowed={false} />,
    );
    expect(screen.getByRole("button", { name: /start voice/i })).toBeInTheDocument();
    // No mic chevron in this mode.
    expect(screen.queryByRole("button", { name: /voice input/i })).toBeNull();
  });

  it("renders nothing when neither dictation nor voice is available", () => {
    const { container } = render(
      <VoiceInputControl onRecorded={vi.fn()} audioInputAllowed={false} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
