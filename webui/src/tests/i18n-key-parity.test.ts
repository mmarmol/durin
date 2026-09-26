import { describe, expect, it } from "vitest";

import en from "@/i18n/locales/en/common.json";
import fr from "@/i18n/locales/fr/common.json";
import id from "@/i18n/locales/id/common.json";
import ja from "@/i18n/locales/ja/common.json";
import ko from "@/i18n/locales/ko/common.json";
import vi from "@/i18n/locales/vi/common.json";
import zhCN from "@/i18n/locales/zh-CN/common.json";
import zhTW from "@/i18n/locales/zh-TW/common.json";

const keysDeep = (o: object, p = ""): string[] =>
  Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === "object" ? keysDeep(v, `${p}${k}.`) : [`${p}${k}`]);

const LOCALES: Record<string, object> = { fr, id, ja, ko, vi, "zh-CN": zhCN, "zh-TW": zhTW };

describe("i18n key parity", () => {
  const expected = keysDeep(en);

  for (const [locale, resource] of Object.entries(LOCALES)) {
    it(`${locale} carries every en key, in the same order`, () => {
      expect(keysDeep(resource)).toEqual(expected);
    });
  }
});
