// webui/src/components/settings/TranscriptionSettings.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ClientProvider } from "@/providers/ClientProvider";
import type { DurinClient } from "@/lib/durin-client";
import { setConfigValue } from "@/lib/api";
import { TranscriptionSettings } from "./TranscriptionSettings";

vi.mock("@/lib/api", () => ({
  getConfig: vi.fn(async () => ({
    config: {
      transcription: { enabled: true, provider: "local", local: { engine: "parakeet" } },
      tts: { enabled: true, provider: "local", local: { voice: "F4" }, language: "es" },
      voice: { enabled: true, barge_in: true, idle_timeout_s: 300, spoken_render: { mode: "model_led", long_threshold_words: 60 } },
    },
    schema: {},
  })),
  setConfigValue: vi.fn(async () => ({})),
  getExtraStatus: vi.fn(async () => ({ present: true, extra: "stt", label: "stt" })),
}));

const fakeClient = {
  onVoicePreviewAudio: () => () => {},
  sendVoicePreview: () => {},
} as unknown as DurinClient;

function renderPane() {
  return render(
    <ClientProvider client={fakeClient} token="tok">
      <TranscriptionSettings token="tok" />
    </ClientProvider>,
  );
}

describe("Voice settings pane", () => {
  it("renders the text-to-speech section", async () => {
    renderPane();
    await waitFor(() => expect(screen.getByText(/text-to-speech/i)).toBeInTheDocument());
    expect(screen.getByText(/spoken rendition/i)).toBeInTheDocument();
  });

  it("saves the idle-timeout selection", async () => {
    renderPane();
    const select = await screen.findByLabelText(/auto-close when idle/i);
    fireEvent.change(select, { target: { value: "60" } });
    await waitFor(() =>
      expect(vi.mocked(setConfigValue)).toHaveBeenCalledWith("tok", "voice.idle_timeout_s", 60),
    );
  });
});
