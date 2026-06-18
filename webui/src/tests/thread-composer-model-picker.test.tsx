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

describe("ThreadComposer — bare /model opens the picker", () => {
  it("intercepts a submitted /model (opens picker, does not send)", () => {
    const onSend = vi.fn();
    render(
      <ThreadComposer
        onSend={onSend}
        onModelPick={vi.fn()}
        placeholder="Type your message..."
      />,
    );
    const input = screen.getByLabelText("Message input");
    fireEvent.change(input, { target: { value: "/model" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    expect(onSend).not.toHaveBeenCalled();
    expect(input).toHaveValue("");
  });

  it("sends /model normally when no onModelPick is wired", () => {
    const onSend = vi.fn();
    render(<ThreadComposer onSend={onSend} placeholder="Type your message..." />);
    const input = screen.getByLabelText("Message input");
    fireEvent.change(input, { target: { value: "/model" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    expect(onSend).toHaveBeenCalledWith("/model", undefined);
  });
});
