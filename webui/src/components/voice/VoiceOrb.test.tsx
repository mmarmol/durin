import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { VoiceOrb } from "./VoiceOrb";

describe("VoiceOrb", () => {
  it("exposes an accessible toggle reflecting state", () => {
    render(<VoiceOrb state="listening" amplitude={0.4} onToggle={() => {}} />);
    const btn = screen.getByRole("button", { name: /voice/i });
    expect(btn).toHaveAttribute("data-state", "listening");
  });

  it("calls onToggle on click", () => {
    const onToggle = vi.fn();
    render(<VoiceOrb state="idle" amplitude={0} onToggle={onToggle} />);
    fireEvent.click(screen.getByRole("button", { name: /voice/i }));
    expect(onToggle).toHaveBeenCalledOnce();
  });
});
