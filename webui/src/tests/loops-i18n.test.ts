import { describe, expect, it } from "vitest";
import en from "@/i18n/locales/en/common.json";
import es from "@/i18n/locales/es/common.json";

const keysDeep = (o: object, p = ""): string[] =>
  Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === "object" ? keysDeep(v, `${p}${k}.`) : [`${p}${k}`]);

describe("loops i18n parity", () => {
  it("es mirrors every en loops.* key", () => {
    expect(keysDeep((es as any).loops ?? {})).toEqual(keysDeep((en as any).loops ?? {}));
  });
  it("loops namespace exists", () => {
    expect((en as any).loops).toBeTruthy();
  });
});
