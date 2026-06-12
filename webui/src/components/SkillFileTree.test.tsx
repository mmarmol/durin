import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SkillFileTree } from "@/components/SkillFileTree";
import type { SkillFile } from "@/lib/api";

describe("SkillFileTree", () => {
  const files: SkillFile[] = [
    { path: "SKILL.md", text: true, size: 120 },
    { path: "scripts/run.sh", text: true, size: 80 },
    { path: "scripts/helpers/util.py", text: true, size: 200 },
    { path: "data/config.json", text: true, size: 60 },
    { path: "binary.bin", text: false, size: 1024 },
  ];

  it("renders top-level files", () => {
    render(<SkillFileTree files={files} onSelect={vi.fn()} />);
    expect(screen.getByText("SKILL.md")).toBeInTheDocument();
    expect(screen.getByText("binary.bin")).toBeInTheDocument();
  });

  it("groups files under their folder segment", () => {
    render(<SkillFileTree files={files} onSelect={vi.fn()} />);
    // folder labels appear
    expect(screen.getByText("scripts")).toBeInTheDocument();
    expect(screen.getByText("data")).toBeInTheDocument();
  });

  it("clicking a top-level file calls onSelect with its path", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<SkillFileTree files={files} onSelect={onSelect} />);
    await user.click(screen.getByText("SKILL.md"));
    expect(onSelect).toHaveBeenCalledWith("SKILL.md");
  });

  it("clicking a file inside a folder calls onSelect with full path", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<SkillFileTree files={files} onSelect={onSelect} />);
    // run.sh is nested under scripts — click the button/row for it
    await user.click(screen.getByText("run.sh"));
    expect(onSelect).toHaveBeenCalledWith("scripts/run.sh");
  });

  it("highlights the selected file", () => {
    const { rerender } = render(
      <SkillFileTree files={files} selected="SKILL.md" onSelect={vi.fn()} />,
    );
    const item = screen.getByText("SKILL.md").closest("button");
    expect(item?.className).toMatch(/primary/);

    rerender(<SkillFileTree files={files} selected="binary.bin" onSelect={vi.fn()} />);
    const item2 = screen.getByText("binary.bin").closest("button");
    expect(item2?.className).toMatch(/primary/);
  });

  it("renders an empty state when no files are passed", () => {
    render(<SkillFileTree files={[]} onSelect={vi.fn()} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
