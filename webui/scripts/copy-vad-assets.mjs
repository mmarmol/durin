import { cpSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const dest = join(root, "public", "vad");
mkdirSync(dest, { recursive: true });

const vadDist = join(root, "node_modules", "@ricky0123", "vad-web", "dist");
for (const f of ["vad.worklet.bundle.min.js", "silero_vad_v5.onnx", "silero_vad_legacy.onnx"]) {
  cpSync(join(vadDist, f), join(dest, f));
}
const ortDist = join(root, "node_modules", "onnxruntime-web", "dist");
for (const f of readdirSync(ortDist).filter((n) => n.endsWith(".wasm") || n.endsWith(".mjs"))) {
  cpSync(join(ortDist, f), join(dest, f));
}
console.log("copied vad assets ->", dest);
