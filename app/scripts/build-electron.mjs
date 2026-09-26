// Bundles the Electron main process and preload script with esbuild.
import { build } from "esbuild";

const common = {
  bundle: true,
  platform: "node",
  target: "node22",
  sourcemap: true,
  external: ["electron"],
  logLevel: "info",
};

await build({ ...common, entryPoints: ["electron/main.ts"], outfile: "dist-electron/main.js", format: "esm" });
// Sandboxed preload scripts must be CommonJS.
await build({ ...common, entryPoints: ["electron/preload.ts"], outfile: "dist-electron/preload.cjs", format: "cjs" });
