import { describe, expect, it } from "vitest";

import en from "@/i18n/locales/en/common.json";
import es from "@/i18n/locales/es/common.json";
import fr from "@/i18n/locales/fr/common.json";
import id from "@/i18n/locales/id/common.json";
import ja from "@/i18n/locales/ja/common.json";
import ko from "@/i18n/locales/ko/common.json";
import vi from "@/i18n/locales/vi/common.json";
import zhCN from "@/i18n/locales/zh-CN/common.json";
import zhTW from "@/i18n/locales/zh-TW/common.json";

describe("workflows i18n parity", () => {
  it("workflows block top-level keys match between en and es", () => {
    expect(Object.keys(en.workflows).sort()).toEqual(Object.keys(es.workflows).sort());
  });

  it("workflows.kind keys match between en and es", () => {
    expect(Object.keys(en.workflows.kind).sort()).toEqual(
      Object.keys(es.workflows.kind).sort(),
    );
  });

  // An interrupted run (its process died; it can be resumed at its step) and an
  // aborted one (a step failed) are told apart by their banner title alone.
  const ALL = { en, es, fr, id, ja, ko, vi, "zh-CN": zhCN, "zh-TW": zhTW };
  for (const [locale, resource] of Object.entries(ALL)) {
    it(`${locale}: the interrupted-run title is not the aborted-run title`, () => {
      expect(resource.workflows.crashedTitle).not.toEqual(resource.workflows.abortedTitle);
    });
  }
});
