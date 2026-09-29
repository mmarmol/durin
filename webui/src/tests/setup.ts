import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";

import i18n from "@/i18n";

// happy-dom doesn't ship with ``crypto.randomUUID``; shim a tiny v4-ish helper.
if (!("randomUUID" in globalThis.crypto)) {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: () =>
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        const v = c === "x" ? r : (r & 0x3) | 0x8;
        return v.toString(16);
      }),
    configurable: true,
  });
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  document.documentElement.lang = "en";
  document.title = "durin";
  localStorage.setItem("durin.locale", "en");
});

// No test reaches the network. A call the test did not fake would go to
// happy-dom's origin (localhost:3000) and fail with a connection error that
// most components swallow, so the test would pass on whatever that failure
// happened to render. Such a call fails the test instead, naming the URL.
// data: and blob: URLs never leave the process and pass through.
const unfakedFetches: string[] = [];
const realFetch = globalThis.fetch;
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (url.startsWith("data:") || url.startsWith("blob:")) return realFetch(input, init);
  unfakedFetches.push(`${init?.method ?? "GET"} ${url}`);
  return Promise.reject(new TypeError(`fetch without a fake in a test: ${url}`));
}) as typeof fetch;

afterEach(() => {
  const calls = unfakedFetches.splice(0);
  if (calls.length > 0) throw new Error(`The test fetched without a fake: ${calls.join(", ")}`);
});
