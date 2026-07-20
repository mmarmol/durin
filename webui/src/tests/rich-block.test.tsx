// webui/src/tests/rich-block.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import i18n from "@/i18n";
import { RichBlock } from "@/components/rich/RichBlock";

const wrap = (ui: React.ReactNode) => <I18nextProvider i18n={i18n}>{ui}</I18nextProvider>;

describe("RichBlock", () => {
  it("shows a download control for a mermaid block", () => {
    render(wrap(<RichBlock language="mermaid" code="graph TD; A-->B" />));
    expect(screen.getByLabelText("Download")).toBeInTheDocument();
    expect(screen.getByLabelText("Expand")).toBeInTheDocument();
  });

  it("opens a fullscreen inspector when a mermaid block is expanded", async () => {
    render(wrap(<RichBlock language="mermaid" code="graph TD; A-->B" />));
    screen.getByLabelText("Expand").click();
    // The inspector's pan/zoom hint is only present in the fullscreen inspector.
    expect(await screen.findByText("scroll to zoom · drag to pan")).toBeInTheDocument();
  });

  it("expands an html sandbox without a zoom inspector", async () => {
    render(wrap(<RichBlock language="html" code="<b>hi</b>" />));
    screen.getByLabelText("Expand").click();
    // Radix Dialog's Presence mounts Dialog.Content on a layout-effect-driven
    // second commit, not synchronously with the click — same as the mermaid
    // case above, so this assertion must also be awaited.
    expect(await screen.findByLabelText("Close")).toBeInTheDocument();
    expect(screen.queryByText("scroll to zoom · drag to pan")).not.toBeInTheDocument();
  });

  it("falls back to a code block for non-rich languages", () => {
    const { container } = render(wrap(<RichBlock language="python" code="print(1)" />));
    expect(container.querySelector("pre")).not.toBeNull();
  });

  it("renders an HTML preview by default in a sandboxed iframe", () => {
    const { container } = render(wrap(<RichBlock language="html" code="<b>hello</b>" />));
    const iframe = container.querySelector("iframe");
    expect(iframe).not.toBeNull();
    expect(iframe!.getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("toggles to the source view", () => {
    const { container } = render(wrap(<RichBlock language="html" code="<b>hello</b>" />));
    fireEvent.click(screen.getByRole("button", { name: "Code" }));
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.textContent).toContain("hello");
  });

  it("wraps SVG source as iframe content", () => {
    const { container } = render(wrap(<RichBlock language="svg" code='<svg><circle r="5"/></svg>' />));
    const iframe = container.querySelector("iframe")!;
    expect(iframe.getAttribute("srcdoc")).toContain("<svg>");
  });
});
