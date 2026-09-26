import { describe, expect, it } from "vitest";

import en from "@/i18n/locales/en/common.json";
import es from "@/i18n/locales/es/common.json";

const keysDeep = (o: object, p = ""): string[] =>
  Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === "object" ? keysDeep(v, `${p}${k}.`) : [`${p}${k}`]);

describe("pending i18n", () => {
  it("es mirrors every en pending.* key", () => {
    expect(keysDeep((es as any).pending ?? {})).toEqual(keysDeep((en as any).pending ?? {}));
  });

  it("the pending namespace names the page and each source", () => {
    expect((en as any).pending?.title).toBeTruthy();
    expect(Object.keys((en as any).pending?.source ?? {})).toEqual([
      "approval",
      "skill_quarantine",
      "automation_run",
      "workflow_run",
      "flagged_pair",
      "skill_suggestion",
    ]);
  });
});
