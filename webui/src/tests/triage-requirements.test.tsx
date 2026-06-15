import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { TriageRequirements } from "../components/TriageRequirements";
import type { SkillRequirements } from "../lib/api";

const req: SkillRequirements = {
  platforms: ["macos"],
  platform_ok: true,
  bins: [
    { name: "gh", available: true },
    { name: "ffmpeg", available: false, installable: true, install_spec: "brew: ffmpeg" },
    { name: "custom", available: false, installable: false },
  ],
  env: [
    { name: "TOKEN", available: true },
    { name: "MISSING", available: false },
  ],
  compatibility: "Needs brew.",
};

describe("TriageRequirements", () => {
  it("shows available bins with checkmark", () => {
    render(<TriageRequirements requirements={req} skillName="test" token="" />);
    expect(screen.getByText("gh")).toBeInTheDocument();
  });

  it("shows Install button for installable missing bin", () => {
    render(<TriageRequirements requirements={req} skillName="test" token="" />);
    expect(screen.getByText("ffmpeg")).toBeInTheDocument();
  });

  it("shows agent fallback for non-installable missing bin", () => {
    render(<TriageRequirements requirements={req} skillName="test" token="" />);
    expect(screen.getByText("custom")).toBeInTheDocument();
  });

  it("shows platform badges", () => {
    render(<TriageRequirements requirements={req} skillName="test" token="" />);
    expect(screen.getByText(/macOS/i)).toBeInTheDocument();
  });

  it("shows compatibility note", () => {
    render(<TriageRequirements requirements={req} skillName="test" token="" />);
    expect(screen.getByText("Needs brew.")).toBeInTheDocument();
  });
});
