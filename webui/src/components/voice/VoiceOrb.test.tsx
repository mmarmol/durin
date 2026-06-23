import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { VoiceOrb } from "./VoiceOrb";

describe("VoiceOrb", () => {
  it("renders a labeled button reflecting state when clickable", () => {
    render(<VoiceOrb state="listening" amplitude={0.4} label="Listening" onClick={() => {}} />);
    const btn = screen.getByRole("button", { name: "Listening" });
    expect(btn).toHaveAttribute("data-state", "listening");
  });

  it("calls onClick on click", () => {
    const onClick = vi.fn();
    render(<VoiceOrb state="idle" amplitude={0} label="Tap to talk" onClick={onClick} />);
    fireEvent.click(screen.getByRole("button", { name: "Tap to talk" }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("renders a bare glyph (no button) when not clickable", () => {
    render(<VoiceOrb state="speaking" amplitude={0.5} label="Speaking" />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("img", { name: "Speaking" })).toBeTruthy();
  });
});
