// Served from the SPA origin (copied into public/vad/ at build), never a CDN —
// durin runs offline-first on localhost, so the Silero/ONNX assets ship locally.
export const VAD_BASE_ASSET_PATH = "/vad/";
export const ONNX_WASM_BASE_PATH = "/vad/";

// The Silero model + ONNX-runtime WASM are the slow part of the first voice
// toggle (multi-MB download + compile). Warming the HTTP cache on app load
// shifts that cost off the user's first mic click. The ORT WASM variant the
// runtime actually picks depends on the browser, so we warm the common ones
// best-effort — an unused one 404s harmlessly. Names track the onnxruntime-web
// build copied by scripts/copy-vad-assets.mjs.
const _PREFETCH_ASSETS = [
  "silero_vad_v5.onnx",
  "vad.worklet.bundle.min.js",
  "ort-wasm-simd-threaded.wasm",
  "ort-wasm-simd-threaded.mjs",
  "ort-wasm-simd-threaded.jsep.wasm",
  "ort-wasm-simd-threaded.jsep.mjs",
];

let _prefetched = false;

/** Best-effort prefetch of the VAD assets so the first voice session is fast.
 * Idempotent and never throws — failures (offline, missing variant) are ignored. */
export function prefetchVoiceAssets(): void {
  if (_prefetched || typeof fetch !== "function") return;
  _prefetched = true;
  for (const f of _PREFETCH_ASSETS) {
    void fetch(`${VAD_BASE_ASSET_PATH}${f}`).catch(() => {});
  }
}
