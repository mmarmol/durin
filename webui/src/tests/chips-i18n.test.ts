import { describe, expect, it } from "vitest";

import { resources } from "@/i18n";

const keysDeep = (o: object, p = ""): string[] =>
  Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === "object" ? keysDeep(v, `${p}${k}.`) : [`${p}${k}`]);

const placeholdersIn = (s: unknown): string[] =>
  typeof s === "string" ? [...s.matchAll(/\{\{(\w+)\}\}/g)].map((m) => m[1]).sort() : [];

describe("message.chips i18n parity", () => {
  const enChips = (resources.en.common as any).message.chips as Record<string, string>;
  const enKeys = keysDeep(enChips);

  for (const [locale, resource] of Object.entries(resources)) {
    if (locale === "en") continue;
    const chips = (resource.common as any).message.chips as Record<string, string>;

    it(`${locale} carries every en message.chips key, minus at most the _one plural variants`, () => {
      // A locale may skip an `_one` key when its plural rule has no
      // singular form (i18next then always resolves `_other`) — anything
      // else missing, or any key the catalog shouldn't have, is a real gap.
      const keys = keysDeep(chips);
      const missing = enKeys.filter((k) => !keys.includes(k) && !k.endsWith("_one"));
      const extra = keys.filter((k) => !enKeys.includes(k));
      expect({ missing, extra }).toEqual({ missing: [], extra: [] });
    });

    it(`${locale} message.chips placeholders match en for every key it carries`, () => {
      for (const key of enKeys) {
        if (!(key in chips)) continue; // documented _one absence, covered above
        expect(placeholdersIn(chips[key])).toEqual(placeholdersIn(enChips[key]));
      }
    });
  }
});

describe("message.chips es register consistency", () => {
  it("memoryForgot matches the block's own past-participle confirmation register", () => {
    // The block's other after-the-fact confirmations ("memoria guardada",
    // "objetivo completado") are past participles; the infinitive
    // "olvidar" broke that pattern.
    const esChips = (resources.es.common as any).message.chips;
    expect(esChips.memoryForgot).toBe("olvidado {{uri}}");
  });
});
