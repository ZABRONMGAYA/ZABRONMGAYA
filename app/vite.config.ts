import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The renderer is loaded from files inside the packaged app, so asset paths are relative.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
    // The app, and the S00 splash shown while the engine starts.
    rollupOptions: { input: { main: "index.html", splash: "splash.html" } },
  },
  test: { environment: "jsdom", include: ["tests/**/*.test.ts?(x)"] },
});
