import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// Only these two modules may call the `fetch` global directly:
//   - http.ts        — `fetchWithReauth`, the single door every API call goes
//                      through; it mints a fresh token and retries once on 401.
//   - bootstrap.ts   — mints/clears the token itself; reauthing it would loop.
// Any other raw fetch() bypasses the 401 reauth-and-retry and silently breaks
// once the bootstrap token expires (~5 min) — the webui-thread regression this
// guards against. New API helpers must import fetchWithReauth, not call fetch.
const ALLOWED = new Set(["src/lib/http.ts", "src/lib/bootstrap.ts"]);

const RAW_FETCH = /\bfetch\s*\(/;

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "tests") continue; // test setup/helpers stub fetch on purpose
      out.push(...sourceFiles(full));
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/[^\n]*/g, "");
}

describe("no raw fetch() outside the auth module", () => {
  it("every API call routes through fetchWithReauth (http.ts)", () => {
    const offenders = sourceFiles("src")
      .map((f) => f.replace(/\\/g, "/"))
      .filter((rel) => !ALLOWED.has(rel))
      .filter((rel) => RAW_FETCH.test(stripComments(readFileSync(rel, "utf8"))));
    expect(offenders).toEqual([]);
  });
});
