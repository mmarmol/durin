// webui/src/components/settings/DocumentsSettings.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getConfig, getExtraStatus, setConfigValue } from "@/lib/api";
import { DocumentsSettings } from "./DocumentsSettings";

vi.mock("@/lib/api", () => ({
  getConfig: vi.fn(async () => ({
    config: {
      documents: {
        ocr: { enabled: false, inline_max_pages: 5, language: null },
        max_file_size_mb: 50,
        max_text_chars: 200000,
      },
    },
    schema: {},
  })),
  setConfigValue: vi.fn(async () => ({
    documents: {
      ocr: { enabled: true, inline_max_pages: 5, language: null },
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
    // Scoped by the row's own accessible name, not DOM position: every
    // Save button in this pane would otherwise share the name "Save", so
    // a reorder of the rows must not silently point this at the wrong one.
    const save = screen.getByRole("button", { name: /save inline page limit/i });
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
    const save = screen.getByRole("button", { name: /save largest file to read/i });
    fireEvent.click(save);
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

  it("renders the language selector on the built-in pack from config", async () => {
    renderCard();
    const select = await screen.findByRole("combobox", {
      name: /recognition language/i,
    });
    // null in config = the built-in pack = the empty option value.
    expect(select).toHaveValue("");
  });

  it("saves a selected recognition language by its config code", async () => {
    renderCard();
    const select = await screen.findByRole("combobox", {
      name: /recognition language/i,
    });
    fireEvent.change(select, { target: { value: "arabic" } });
    await waitFor(() =>
      expect(vi.mocked(setConfigValue)).toHaveBeenCalledWith(
        "tok",
        "documents.ocr.language",
        "arabic",
      ),
    );
  });

  it("saves null when switching back to the built-in pack", async () => {
    vi.mocked(getConfig).mockResolvedValueOnce({
      config: {
        documents: {
          ocr: { enabled: true, inline_max_pages: 5, language: "el" },
          max_file_size_mb: 50,
          max_text_chars: 200000,
        },
      },
      schema: {},
    });
    renderCard();
    const select = await screen.findByRole("combobox", {
      name: /recognition language/i,
    });
    expect(select).toHaveValue("el");
    fireEvent.change(select, { target: { value: "" } });
    await waitFor(() =>
      expect(vi.mocked(setConfigValue)).toHaveBeenCalledWith(
        "tok",
        "documents.ocr.language",
        null,
      ),
    );
  });

  it("tells the truth about the one-time model download near the selector", async () => {
    const { container } = renderCard();
    await screen.findByRole("combobox", { name: /recognition language/i });
    expect(container.textContent?.toLowerCase()).toContain("modelscope.cn");
    expect(container.textContent?.toLowerCase()).toContain("once");
  });
});
