import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SandboxFrame } from "@/components/rich/SandboxFrame";

describe("SandboxFrame", () => {
  it("sandboxes scripts without same-origin and blocks network", () => {
    const { container } = render(
      <SandboxFrame html="<p>hi</p>" title="preview" />,
    );
    const iframe = container.querySelector("iframe")!;
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
    const doc = iframe.getAttribute("srcdoc") ?? "";
    expect(doc).toContain("<p>hi</p>");
    expect(doc).toContain("connect-src 'none'");
  });
});
