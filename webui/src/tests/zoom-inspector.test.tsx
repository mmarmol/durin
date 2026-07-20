// webui/src/tests/zoom-inspector.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { ZoomInspector } from "@/components/rich/ZoomInspector";

function renderInspector(props: Partial<{ onDownload: () => void; onClose: () => void }> = {}) {
  return render(
    <I18nextProvider i18n={i18n}>
      <ZoomInspector onDownload={props.onDownload ?? vi.fn()} onClose={props.onClose ?? vi.fn()}>
        <svg viewBox="0 0 800 200" />
      </ZoomInspector>
    </I18nextProvider>,
  );
}

describe("ZoomInspector", () => {
  it("exposes zoom, fit, download and close controls", () => {
    renderInspector();
    expect(screen.getByLabelText("Zoom in")).toBeInTheDocument();
    expect(screen.getByLabelText("Zoom out")).toBeInTheDocument();
    expect(screen.getByLabelText("Fit to screen")).toBeInTheDocument();
    expect(screen.getByLabelText("Download")).toBeInTheDocument();
    expect(screen.getByLabelText("Close")).toBeInTheDocument();
  });

  it("shows a zoom percentage readout", () => {
    renderInspector();
    expect(screen.getByText("100%")).toBeInTheDocument();
  });

  it("calls onClose when the close button is clicked", async () => {
    const onClose = vi.fn();
    renderInspector({ onClose });
    screen.getByLabelText("Close").click();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("calls onDownload when the download button is clicked", () => {
    const onDownload = vi.fn();
    renderInspector({ onDownload });
    screen.getByLabelText("Download").click();
    expect(onDownload).toHaveBeenCalledOnce();
  });
});
