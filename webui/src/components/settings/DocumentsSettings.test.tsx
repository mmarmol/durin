// webui/src/components/settings/DocumentsSettings.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getExtraStatus, setConfigValue } from "@/lib/api";
import { DocumentsSettings } from "./DocumentsSettings";

vi.mock("@/lib/api", () => ({
  getConfig: vi.fn(async () => ({
    config: {
      documents: {
        ocr: { enabled: false, inline_max_pages: 5 },
        max_file_size_mb: 50,
        max_text_chars: 200000,
      },
    },
    schema: {},
  })),
  setConfigValue: vi.fn(async () => ({
    documents: {
      ocr: { enabled: true, inline_max_pages: 5 },
      max_file_size_mb: 50,
      max_text_chars: 200000,
    },
  })),
  getExtraStatus: vi.fn(async () => ({
    present: true,
    extra: "ocr",
    approx_size: "~90 MB", // arbitrary fixture text; the real figure is measured in Task 10
    needs_restart: true,
    label: "Local OCR (scanned PDFs)",
  })),
  ensureExtra: vi.fn(async () => ({ status: "installed", restarting: false })),
}));

function renderCard() {
  return render(<DocumentsSettings token="tok" />);
}

describe("DocumentsSettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the OCR switch off from config", async () => {
    renderCard();
    const toggle = await screen.findByRole("switch", { name: /ocr/i });
    expect(toggle).not.toBeChecked();
  });

  it("saves the toggle when the extra is already installed", async () => {
    renderCard();
    const toggle = await screen.findByRole("switch", { name: /ocr/i });
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(vi.mocked(setConfigValue)).toHaveBeenCalledWith(
        "tok",
        "documents.ocr.enabled",
        true,
      ),
    );
  });

  it("asks to install the extra before enabling when it is missing", async () => {
    vi.mocked(getExtraStatus).mockResolvedValueOnce({
      present: false,
      extra: "ocr",
      approx_size: "~90 MB", // arbitrary fixture text; the real figure is measured in Task 10
      needs_restart: true,
      label: "Local OCR (scanned PDFs)",
    });
    renderCard();
    const toggle = await screen.findByRole("switch", { name: /ocr/i });
    fireEvent.click(toggle);

    // The install confirm appears and the config is NOT written yet: turning
    // the switch on without the engine present would be a lie.
    await screen.findByRole("button", { name: /install/i });
    expect(vi.mocked(setConfigValue)).not.toHaveBeenCalled();
  });

  it("saves an edited inline page limit", async () => {
    renderCard();
    const input = await screen.findByDisplayValue("5");
    fireEvent.change(input, { target: { value: "20" } });
    const save = screen.getAllByRole("button", { name: /save/i })[0];
    fireEvent.click(save);
    await waitFor(() =>
      expect(vi.mocked(setConfigValue)).toHaveBeenCalledWith(
        "tok",
        "documents.ocr.inline_max_pages",
        20,
      ),
    );
  });

  it("saves an edited file size limit", async () => {
    renderCard();
    const input = await screen.findByDisplayValue("50");
    fireEvent.change(input, { target: { value: "120" } });
    const saves = screen.getAllByRole("button", { name: /save/i });
    fireEvent.click(saves[saves.length - 1]);
    await waitFor(() =>
      expect(vi.mocked(setConfigValue)).toHaveBeenCalledWith(
        "tok",
        "documents.max_file_size_mb",
        120,
      ),
    );
  });

  it("never names the conversion library in the UI", async () => {
    const { container } = renderCard();
    await screen.findByRole("switch", { name: /ocr/i });
    expect(container.textContent?.toLowerCase()).not.toContain("markitdown");
  });
});
