import { renderHook, act } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useAttachedAudio } from "./useAttachedAudio";

function wavFile(name = "a.wav", size = 1000): File {
  const blob = new Blob([new Uint8Array(size)], { type: "audio/wav" });
  return new File([blob], name, { type: "audio/wav" });
}

describe("useAttachedAudio", () => {
  it("accepts a supported audio file", () => {
    const { result } = renderHook(() => useAttachedAudio());
    act(() => {
      result.current.enqueue([wavFile()]);
    });
    expect(result.current.audio).toHaveLength(1);
    expect(result.current.audio[0].status).toBe("ready");
  });

  it("rejects unsupported MIME", () => {
    const { result } = renderHook(() => useAttachedAudio());
    const bad = new File([new Blob(["x"])], "a.txt", { type: "text/plain" });
    let rejected: { rejected: unknown } | undefined;
    act(() => {
      rejected = result.current.enqueue([bad]) as { rejected: unknown };
    });
    expect((rejected as { rejected: unknown[] }).rejected).toHaveLength(1);
    expect(result.current.audio).toHaveLength(0);
  });

  it("enforces a single attachment cap", () => {
    const { result } = renderHook(() => useAttachedAudio());
    act(() => {
      result.current.enqueue([wavFile("a.wav")]);
    });
    let rejected: { rejected: unknown } | undefined;
    act(() => {
      rejected = result.current.enqueue([wavFile("b.wav")]) as {
        rejected: unknown;
      };
    });
    expect((rejected as { rejected: unknown[] }).rejected).toHaveLength(1);
    expect(result.current.audio).toHaveLength(1);
  });

  it("rejects files over the size cap", () => {
    const { result } = renderHook(() => useAttachedAudio());
    // 30MB exceeds the 25MB cap.
    const huge = new File([new Uint8Array(30 * 1024 * 1024)], "big.wav", {
      type: "audio/wav",
    });
    let rejected: { rejected: unknown } | undefined;
    act(() => {
      rejected = result.current.enqueue([huge]) as { rejected: unknown };
    });
    expect((rejected as { rejected: unknown[] }).rejected).toHaveLength(1);
    expect(result.current.audio).toHaveLength(0);
  });

  it("remove clears a specific attachment", () => {
    const { result } = renderHook(() => useAttachedAudio());
    act(() => {
      result.current.enqueue([wavFile("a.wav")]);
    });
    const id = result.current.audio[0].id;
    act(() => {
      result.current.remove(id);
    });
    expect(result.current.audio).toHaveLength(0);
  });

  it("clear empties all attachments", () => {
    const { result } = renderHook(() => useAttachedAudio());
    act(() => {
      result.current.enqueue([wavFile("a.wav")]);
    });
    act(() => {
      result.current.clear();
    });
    expect(result.current.audio).toHaveLength(0);
  });

  it("setStatus transitions: enqueue → pending → downloading → transcribing → ready", () => {
    const { result } = renderHook(() => useAttachedAudio());
    act(() => {
      result.current.enqueue([wavFile("a.wav")]);
    });
    expect(result.current.audio[0].status).toBe("ready");
    const id = result.current.audio[0].id;
    act(() => {
      result.current.setStatus(id, "pending");
    });
    expect(result.current.audio[0].status).toBe("pending");
    act(() => {
      result.current.setStatus(id, "downloading");
    });
    expect(result.current.audio[0].status).toBe("downloading");
    act(() => {
      result.current.setStatus(id, "transcribing");
    });
    expect(result.current.audio[0].status).toBe("transcribing");
    act(() => {
      result.current.setStatus(id, "ready");
    });
    expect(result.current.audio[0].status).toBe("ready");
  });

  it("setStatus is a no-op for unknown ids", () => {
    const { result } = renderHook(() => useAttachedAudio());
    act(() => {
      result.current.enqueue([wavFile("a.wav")]);
    });
    act(() => {
      result.current.setStatus("nonexistent-id", "transcribing");
    });
    // Original attachment is untouched
    expect(result.current.audio[0].status).toBe("ready");
  });

  it("enqueue returns accepted items with ids", () => {
    const { result } = renderHook(() => useAttachedAudio());
    let accepted: typeof result.current.audio = [];
    act(() => {
      const res = result.current.enqueue([wavFile("a.wav")]);
      accepted = res.accepted;
    });
    expect(accepted).toHaveLength(1);
    expect(accepted[0].id).toBeTruthy();
    expect(accepted[0].status).toBe("ready");
  });
});
