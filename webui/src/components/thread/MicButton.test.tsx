import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MicButton } from "./MicButton";

describe("MicButton", () => {
  const realMR =
    (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder;

  beforeEach(() => {
    (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder =
      class {
        static isTypeSupported() {
          return true;
        }
        state = "inactive";
        onstop: (() => void) | null = null;
        ondataavailable:
          | ((e: { data: { size: number } }) => void)
          | null = null;
        mimeType = "audio/webm";
        chunks: unknown[] = [];
        start() {
          this.state = "recording";
        }
        stop() {
          this.state = "inactive";
          this.onstop?.();
        }
      } as unknown as typeof MediaRecorder;
    // happy-dom's MediaStream lacks getTracks(); give it one.
    const fakeStream = {
      getTracks: () => [{ stop: vi.fn() }],
    };
    (navigator as Navigator & {
      mediaDevices?: MediaDevices;
    }).mediaDevices = {
      getUserMedia: vi.fn().mockResolvedValue(fakeStream),
    } as unknown as MediaDevices;
  });

  afterEach(() => {
    if (realMR) {
      (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder =
        realMR;
    }
  });

  it("renders a mic button", () => {
    render(<MicButton onRecorded={vi.fn()} />);
    expect(screen.getByRole("button", { name: /mic/i })).toBeTruthy();
  });

  it("disables when MediaRecorder is absent", () => {
    delete (globalThis as { MediaRecorder?: typeof MediaRecorder })
      .MediaRecorder;
    render(<MicButton onRecorded={vi.fn()} />);
    const btn = screen.getByRole("button", { name: /mic/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("starts recording on click, then stops and calls onRecorded", async () => {
    const user = userEvent.setup();
    const onRecorded = vi.fn();
    render(<MicButton onRecorded={onRecorded} />);
    const btn = screen.getByRole("button", { name: /mic/i });

    await user.click(btn); // start
    expect(btn.textContent).toMatch(/⏹|stop/);
    await user.click(btn); // stop
    expect(onRecorded).toHaveBeenCalledTimes(1);
    const file = onRecorded.mock.calls[0][0] as File;
    expect(file.type.startsWith("audio/")).toBe(true);
  });

  it("shows an error when mic permission is denied", async () => {
    (navigator as Navigator & { mediaDevices?: MediaDevices }).mediaDevices = {
      getUserMedia: vi.fn().mockRejectedValue(new Error("denied")),
    } as unknown as MediaDevices;
    render(<MicButton onRecorded={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /mic/i }));
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});
