import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ComposerAddMenu } from "@/components/thread/ComposerAddMenu";

describe("ComposerAddMenu", () => {
  it("opens the menu and routes attach / slash actions", () => {
    const onAttach = vi.fn();
    const onSlash = vi.fn();
    render(<ComposerAddMenu onAttach={onAttach} onSlash={onSlash} />);

    // Menu is collapsed until the "+" is clicked.
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    fireEvent.click(screen.getByRole("menuitem", { name: /upload file or photo/i }));
    expect(onAttach).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /slash commands/i }));
    expect(onSlash).toHaveBeenCalledTimes(1);
  });

  it("offers the equation row only when onEquation is provided", () => {
    const onEquation = vi.fn();
    const { rerender } = render(<ComposerAddMenu onAttach={vi.fn()} onSlash={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.queryByRole("menuitem", { name: /insert equation/i })).toBeNull();

    rerender(<ComposerAddMenu onAttach={vi.fn()} onSlash={vi.fn()} onEquation={onEquation} />);
    fireEvent.click(screen.getByRole("menuitem", { name: /insert equation/i }));
    expect(onEquation).toHaveBeenCalledTimes(1);
  });

  it("disables the attach row when attachments are full", () => {
    const onAttach = vi.fn();
    render(<ComposerAddMenu onAttach={onAttach} onSlash={vi.fn()} attachDisabled />);
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    const attach = screen.getByRole("menuitem", { name: /upload file or photo/i });
    expect(attach).toBeDisabled();
    fireEvent.click(attach);
    expect(onAttach).not.toHaveBeenCalled();
  });
});
