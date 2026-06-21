import { render } from "@testing-library/react";
import { expect, it } from "vitest";

import { linkify } from "@/lib/linkify";

it("linkifies full URLs", () => {
  const { container } = render(<span>{linkify("Get a key at https://acme.dev/keys now")}</span>);
  const a = container.querySelector("a");
  expect(a?.getAttribute("href")).toBe("https://acme.dev/keys");
});

it("linkifies bare allowlisted domains with https", () => {
  const { container } = render(<span>{linkify("obtain at inferventis.ai")}</span>);
  expect(container.querySelector("a")?.getAttribute("href")).toBe("https://inferventis.ai");
});

it("leaves plain text untouched", () => {
  const { container } = render(<span>{linkify("Set an environment variable")}</span>);
  expect(container.querySelector("a")).toBeNull();
});

it("does not linkify filenames with .sh extension", () => {
  const { container } = render(<span>{linkify("Run install.sh to get started")}</span>);
  expect(container.querySelector("a")).toBeNull();
});
