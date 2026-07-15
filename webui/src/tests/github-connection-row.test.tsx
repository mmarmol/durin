import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GitHubConnectionRow } from "@/components/settings/GitHubConnectionCard";
import { fetchGithubStatus, pollGithubDeviceFlow } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchGithubStatus: vi.fn(() => Promise.resolve({ connected: false })),
  startGithubDeviceFlow: vi.fn(() =>
    Promise.resolve({
      flow_id: "f1",
      user_code: "AB-12",
      verification_uri: "https://gh/dev",
      verification_uri_complete: "https://gh/dev?c=AB-12",
      interval: 5,
      expires_in: 900,
    }),
  ),
  pollGithubDeviceFlow: vi.fn(),
}));

const poll = vi.mocked(pollGithubDeviceFlow);
const status = vi.mocked(fetchGithubStatus);

async function startFlow() {
  vi.useFakeTimers();
  vi.stubGlobal("open", vi.fn());
  render(<GitHubConnectionRow token="tok" />);
  // initial status probe resolves -> "Connect" button appears
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  await act(async () => {});
  expect(screen.getByText("AB-12")).toBeInTheDocument();
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("GitHubConnectionRow device-flow polling", () => {
  it("survives a transient poll failure and completes on the next poll", async () => {
    poll
      .mockRejectedValueOnce(new Error("gateway restarting"))
      .mockResolvedValueOnce({ status: "authorized" });
    status
      .mockResolvedValueOnce({ connected: false })
      .mockResolvedValue({ connected: true, reachable: true, login: "marcelo", source: "secret" });

    await startFlow();

    // first poll fails -> the flow must stay alive and keep the code visible
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(poll).toHaveBeenCalledTimes(1);
    expect(screen.getByText("AB-12")).toBeInTheDocument();

    // second poll authorizes -> challenge cleared, connected status shown
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(poll).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("AB-12")).not.toBeInTheDocument();
    expect(screen.getByText("@marcelo")).toBeInTheDocument();
  });

  it("gives up visibly after persistent failures: no zombie waiting spinner", async () => {
    status.mockResolvedValue({ connected: false });
    poll.mockRejectedValue(new Error("gateway down"));

    await startFlow();

    // ride through the tolerated failures, then the loop must abort cleanly
    for (let i = 0; i < 6; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
    }
    expect(poll).toHaveBeenCalledTimes(6);
    expect(screen.queryByText("AB-12")).not.toBeInTheDocument();
    expect(screen.getByText("gateway down")).toBeInTheDocument();

    // and nothing keeps polling behind the scenes
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(poll).toHaveBeenCalledTimes(6);
  });
});
