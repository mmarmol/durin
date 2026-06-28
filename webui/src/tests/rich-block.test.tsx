// webui/src/tests/rich-block.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RichBlock } from "@/components/rich/RichBlock";

describe("RichBlock", () => {
  it("renders an HTML preview by default in a sandboxed iframe", () => {
    const { container } = render(
      <RichBlock language="html" code="<b>hello</b>" />,
    );
    const iframe = container.querySelector("iframe");
    expect(iframe).not.toBeNull();
    expect(iframe!.getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("toggles to the source view", () => {
    const { container } = render(
      <RichBlock language="html" code="<b>hello</b>" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Code" }));
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.textContent).toContain("hello");
  });

  it("wraps SVG source as iframe content", () => {
    const { container } = render(
      <RichBlock language="svg" code='<svg><circle r="5"/></svg>' />,
    );
    const iframe = container.querySelector("iframe")!;
    expect(iframe.getAttribute("srcdoc")).toContain("<svg>");
  });

  it("offers an expand control", () => {
    render(<RichBlock language="html" code="<b>x</b>" />);
    expect(screen.getByRole("button", { name: "Expand" })).toBeInTheDocument();
  });
});
