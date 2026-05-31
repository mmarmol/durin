import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import MarkdownTextRenderer from "@/components/MarkdownTextRenderer";

describe("MarkdownTextRenderer wikilinks", () => {
  it("renders [[memory/...]] as a wiki-link anchor", () => {
    render(
      <MarkdownTextRenderer>
        {"See [[memory/episodic/abc]] for context."}
      </MarkdownTextRenderer>,
    );
    const link = screen.getByText("memory/episodic/abc");
    expect(link.tagName).toBe("A");
    expect(link).toHaveClass("wiki-link");
    expect(link.getAttribute("href")).toBe("#wikilink:memory/episodic/abc");
  });

  it("invokes onWikiLinkClick with the target when a handler is provided", () => {
    const handler = vi.fn();
    render(
      <MarkdownTextRenderer onWikiLinkClick={handler}>
        {"Open [[memory/episodic/xyz]]"}
      </MarkdownTextRenderer>,
    );
    const link = screen.getByText("memory/episodic/xyz");
    fireEvent.click(link);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith("memory/episodic/xyz");
  });

  it("renders inert anchor when no handler is registered (no cross-app navigation)", () => {
    // Without onWikiLinkClick the anchor must NOT navigate to any
    // app path — it just gets the `#wikilink:` fragment href so a
    // stray click is a harmless URL-fragment update, never a real
    // route change. (We can't assert pathname stays untouched in
    // happy-dom because clicking a fragment anchor legitimately
    // updates window.location.hash; the contract we care about is
    // that the href is the safe fragment, never a real path.)
    render(
      <MarkdownTextRenderer>
        {"Plain [[memory/episodic/dead]] link."}
      </MarkdownTextRenderer>,
    );
    const link = screen.getByText("memory/episodic/dead");
    expect(link.getAttribute("href")).toBe("#wikilink:memory/episodic/dead");
    expect(link.getAttribute("target")).not.toBe("_blank");
  });

  it("plain markdown links keep their external-link behaviour (no regression)", () => {
    render(
      <MarkdownTextRenderer>
        {"[website](https://example.com)"}
      </MarkdownTextRenderer>,
    );
    const link = screen.getByText("website");
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("href")).toBe("https://example.com");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link).not.toHaveClass("wiki-link");
  });

  it("supports the [[target|alias]] form (alias is the displayed text)", () => {
    render(
      <MarkdownTextRenderer>
        {"See [[memory/episodic/abc|the relevant observation]]."}
      </MarkdownTextRenderer>,
    );
    const link = screen.getByText("the relevant observation");
    expect(link.tagName).toBe("A");
    expect(link).toHaveClass("wiki-link");
    expect(link.getAttribute("href")).toBe("#wikilink:memory/episodic/abc");
  });
});
