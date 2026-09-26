// Development: Vite serves the renderer with hot reload; Electron runs the freshly bundled main process.
// The engine runs from source (`python -m mcsync.cli serve`), so install it first: pip install -e ../engine
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

import { createServer } from "vite";

const require = createRequire(import.meta.url);
const electron = require("electron"); // the path of the Electron binary

await import("./build-electron.mjs");
const server = await createServer({ server: { port: 5173, strictPort: false } });
await server.listen();
const url = server.resolvedUrls?.local[0] ?? "http://localhost:5173/";

const app = spawn(electron, ["."], {
  stdio: "inherit",
  env: { ...process.env, MCSYNC_RENDERER_URL: url },
});
app.on("exit", async (code) => {
  await server.close();
  process.exit(code ?? 0);
});
