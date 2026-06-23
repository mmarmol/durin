// webui/src/components/settings/TranscriptionSettings.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TranscriptionSettings } from "./TranscriptionSettings";

vi.mock("@/lib/api", () => ({
  getConfig: vi.fn(async () => ({
    config: {
      transcription: { enabled: true, provider: "local", local: { engine: "parakeet" } },
      tts: { enabled: true, provider: "local", local: { voice: "F4" }, language: "es" },
      voice: { enabled: true, barge_in: true, spoken_render: { mode: "model_led", long_threshold_words: 60 } },
    },
    schema: {},
  })),
  setConfigValue: vi.fn(async () => ({})),
  getExtraStatus: vi.fn(async () => ({ present: true, extra: "stt", label: "stt" })),
}));

describe("Voice settings pane", () => {
  it("renders the text-to-speech section", async () => {
    render(<TranscriptionSettings token="tok" />);
    await waitFor(() => expect(screen.getByText(/text-to-speech/i)).toBeInTheDocument());
    expect(screen.getByText(/spoken rendition/i)).toBeInTheDocument();
  });
});
