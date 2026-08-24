import { describe, expect, it } from "vitest";
import en from "@/i18n/locales/en/common.json";
import es from "@/i18n/locales/es/common.json";

const keysDeep = (o: object, p = ""): string[] =>
  Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === "object" ? keysDeep(v, `${p}${k}.`) : [`${p}${k}`]);

describe("automations i18n parity", () => {
  it("es mirrors every en automations.* key", () => {
    expect(keysDeep((es as any).automations ?? {})).toEqual(keysDeep((en as any).automations ?? {}));
  });
  it("automations namespace exists", () => {
    expect((en as any).automations).toBeTruthy();
  });
});

describe("loops i18n removed", () => {
  it("en has no loops namespace", () => {
    expect((en as any).loops).toBeUndefined();
  });
  it("es has no loops namespace", () => {
    expect((es as any).loops).toBeUndefined();
  });
});
