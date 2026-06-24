import { describe, expect, it } from "vitest";

import en from "@/i18n/locales/en/common.json";
import es from "@/i18n/locales/es/common.json";

describe("workflows i18n parity", () => {
  it("workflows block top-level keys match between en and es", () => {
    expect(Object.keys(en.workflows).sort()).toEqual(Object.keys(es.workflows).sort());
  });

  it("workflows.kind keys match between en and es", () => {
    expect(Object.keys(en.workflows.kind).sort()).toEqual(
      Object.keys(es.workflows.kind).sort(),
    );
  });
});
