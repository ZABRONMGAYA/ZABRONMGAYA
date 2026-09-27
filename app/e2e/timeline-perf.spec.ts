// Timeline frame rate with a large project (the M4 exit criterion: 60 fps with 300 clips).
// The app runs against e2e/fake-engine.mjs: 3 hours, a recorder and 300 camera clips on 12 devices.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { type ElectronApplication, type Page, _electron as electron, expect, test } from "@playwright/test";

const appDir = path.resolve(import.meta.dirname, "..");
const fakeEngine = path.join(appDir, "e2e", "fake-engine.mjs");
const screens = path.join(appDir, "test-results", "screens");

let work: string;
let app: ElectronApplication;
let page: Page;

test.skip(process.platform === "win32", "the stand-in engine is a script with a shebang");
test.describe.configure({ mode: "serial" });

test.beforeAll(async () => {
  work = fs.mkdtempSync(path.join(os.tmpdir(), "mcsync-perf-"));
  fs.chmodSync(fakeEngine, 0o755);
  app = await electron.launch({
    args: [appDir],
    env: {
      ...process.env,
      MCSYNC_ENGINE: fakeEngine,
      MCSYNC_NO_SANDBOX: "1",
      MCSYNC_CACHE_DIR: path.join(work, "cache"),
      MCSYNC_USER_DATA: path.join(work, "user-data"),
    },
  });
  page = await app.firstWindow();
  await page.setViewportSize({ width: 1440, height: 900 });
  fs.mkdirSync(screens, { recursive: true });
});

test.afterAll(async () => {
  await app?.close();
  fs.rmSync(work, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 });
});

/** Frame intervals (ms) while one wheel event per frame pans or zooms the timeline. */
function measure(mode: "pan" | "zoom", frames: number): Promise<number[]> {
  return page.evaluate(
    ([mode, frames]) =>
      new Promise<number[]>((resolve) => {
        const area = document.querySelector<HTMLElement>('[data-testid="track-area"]')!;
        const rect = area.getBoundingClientRect();
        const intervals: number[] = [];
        let last = performance.now();
        let n = 0;
        const tick = (now: number) => {
          intervals.push(now - last);
          last = now;
          if (n++ >= frames) {
            resolve(intervals.slice(2));
            return;
          }
          const zoom = mode === "zoom";
          area.dispatchEvent(
            new WheelEvent("wheel", {
              deltaX: zoom ? 0 : n % 120 < 60 ? 8 : -8, // back and forth over the footage
              deltaY: zoom ? (n % 60 < 30 ? -60 : 60) : 0,
              ctrlKey: zoom,
              clientX: rect.left + rect.width / 2,
              clientY: rect.top + 60,
              bubbles: true,
              cancelable: true,
            }),
          );
          requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      }),
    [mode, frames] as const,
  );
}

function summary(intervals: number[]): { p50: number; p95: number; fps: number } {
  const sorted = [...intervals].sort((a, b) => a - b);
  const at = (q: number) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))]!;
  const mean = intervals.reduce((a, b) => a + b, 0) / intervals.length;
  return { p50: Math.round(at(0.5) * 10) / 10, p95: Math.round(at(0.95) * 10) / 10, fps: Math.round(1000 / mean) };
}

test("pans and zooms a 300-clip timeline smoothly", async () => {
  await app.evaluate(
    ({ dialog }, file) => {
      dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [file] })) as typeof dialog.showOpenDialog;
    },
    path.join(work, "Benchmark.mcsync"),
  );
  await page.getByTestId("open-project").click();
  await expect(page.getByTestId("media-summary")).toContainText("301 clips");
  await page.getByTestId("stage-timeline").click();
  await expect(page.getByTestId("clip-ZOOM0001.WAV")).toBeAttached();
  const drawn = () => page.evaluate(() => window.mcsyncTimeline?.stats() ?? { clips: 0, waveforms: 0 });
  await expect.poll(async () => (await drawn()).waveforms).toBe(301); // every waveform file has loaded
  await page.screenshot({ path: path.join(screens, "perf-300-clips.png") });

  const results: Record<string, ReturnType<typeof summary>> = {};
  results["pan, whole day visible"] = summary(await measure("pan", 180));
  results["zoom, whole day visible"] = summary(await measure("zoom", 180));
  await page.getByRole("button", { name: "Fit" }).click();
  for (let i = 0; i < 6; i++) await page.getByRole("button", { name: "+", exact: true }).click();
  results["pan, zoomed in"] = summary(await measure("pan", 180));
  expect((await drawn()).clips).toBeGreaterThan(5); // still over the footage
  await page.screenshot({ path: path.join(screens, "perf-zoomed.png") });

  console.log("Timeline frame intervals (ms):");
  console.table(results);
  test.info().annotations.push({ type: "frame intervals", description: JSON.stringify(results) });
  // A guard against pathological regressions only: CI machines render in software.
  for (const r of Object.values(results)) expect(r.p50).toBeLessThan(100);
});
