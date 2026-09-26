import { defineConfig } from "@playwright/test";

// End-to-end tests drive the real Electron app and the real engine (see e2e/README.md).
export default defineConfig({
  testDir: "e2e",
  timeout: 300_000,
  expect: { timeout: 30_000 },
  workers: 1,
  reporter: [["list"]],
  outputDir: "test-results",
});
