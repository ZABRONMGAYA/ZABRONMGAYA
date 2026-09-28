// The whole editor workflow in the real app: new project → import a card dump → synchronise → results → timeline →
// drag a clip → undo → nudge and snap back to the audio → export → reopen the project.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { type ElectronApplication, type Page, _electron as electron, expect, test } from "@playwright/test";

import { closeApp, mainWindow, printEngineLog } from "./helpers";

const appDir = path.resolve(import.meta.dirname, "..");
const python = process.env.MCSYNC_PYTHON ?? (process.platform === "win32" ? "python" : "python3");
const screens = path.join(appDir, "test-results", "screens");

const GENERATE = `
import json, sys
from pathlib import Path
from mcsync.testing.media import generate_wedding_shoot
shoot = generate_wedding_shoot(Path(sys.argv[1]))
print(json.dumps({Path(rel).name: shoot.expected(rel) for rel in shoot.truth}))
`;

let work: string;
let shootDir: string;
let expected: Record<string, number>;
let app: ElectronApplication;
let page: Page;

test.describe.configure({ mode: "serial" });

/** MCSYNC_E2E_APP: run against a packaged app (its executable) instead of the development build. */
const packagedApp = process.env.MCSYNC_E2E_APP;

async function launch(): Promise<void> {
  app = await electron.launch({
    ...(packagedApp ? { executablePath: packagedApp } : { args: [appDir] }),
    env: {
      ...process.env,
      MCSYNC_NO_SANDBOX: "1",
      MCSYNC_CACHE_DIR: path.join(work, "cache"),
      MCSYNC_USER_DATA: path.join(work, "user-data"),
    },
  });
  page = await mainWindow(app);
  await page.setViewportSize({ width: 1440, height: 900 });
}

/** Replace the native file dialogs with fixed answers. */
async function answerDialogs(answers: { save?: string; open?: string[] }): Promise<void> {
  await app.evaluate(({ dialog }, { save, open }) => {
    dialog.showSaveDialog = (async () => ({ canceled: !save, filePath: save ?? "" })) as typeof dialog.showSaveDialog;
    dialog.showOpenDialog = (async () => ({ canceled: !open, filePaths: open ?? [] })) as typeof dialog.showOpenDialog;
  }, answers);
}

async function shot(name: string): Promise<void> {
  await page.screenshot({ path: path.join(screens, `${name}.png`) });
}

/** Where the timeline canvas drew a clip, in page coordinates. */
async function clipBox(name: string): Promise<{ x: number; y: number; width: number; height: number }> {
  const box = await page.evaluate((n) => window.mcsyncTimeline?.clipRect(n) ?? null, name);
  expect(box, `${name} is on screen`).not.toBeNull();
  return box!;
}

async function clickClip(name: string): Promise<void> {
  const box = await clipBox(name);
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
}

async function startOf(name: string): Promise<number> {
  return Number(await page.getByTestId(`clip-${name}`).getAttribute("data-start"));
}

test.beforeAll(() => {
  work = fs.mkdtempSync(path.join(os.tmpdir(), "syncora-e2e-"));
  shootDir = path.join(work, "Smith wedding");
  expected = JSON.parse(execFileSync(python, ["-c", GENERATE, shootDir], { encoding: "utf-8" })) as Record<
    string,
    number
  >;
  fs.mkdirSync(screens, { recursive: true });
});

// A failing step leaves a picture of the window (error toasts included) in test-results.
test.afterEach(async ({}, testInfo) => {
  if (testInfo.status !== testInfo.expectedStatus && page) {
    printEngineLog(path.join(work, "user-data"), `"${testInfo.title}" failed`);
    await page.screenshot({ path: testInfo.outputPath("failure.png") }).catch(() => undefined);
  }
});

test.afterAll(async () => {
  if (app) await closeApp(app, path.join(work, "user-data")).catch(() => undefined);
  // Windows may hold the project file open for a moment after the engine exits.
  fs.rmSync(work, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 });
});

test("starts on the Syncora home screen with the engine ready", async () => {
  await launch();
  await expect(page.getByTestId("welcome")).toBeVisible();
  await expect(page.getByTestId("new-project")).toBeEnabled();
  await expect(page.getByTestId("statusbar")).toContainText("FFmpeg");
  await expect(page).toHaveTitle("Syncora");
  // A packaged app runs its own engine with its own FFmpeg (engine/packaging/build_ffmpeg.sh).
  if (packagedApp) await expect(page.getByTestId("statusbar")).toContainText(/FFmpeg n\d/);
  await shot("01-home");
});

test("creates a project and imports a folder of footage", async () => {
  await answerDialogs({ save: path.join(work, "Smith.syncora"), open: [shootDir] });
  await page.getByTestId("new-project").click();
  await expect(page.getByTestId("toolbar")).toContainText("Smith");
  await expect(page.getByTestId("import-panel")).toBeVisible(); // a new project opens on the import view

  await page.getByTestId("import-folder").click();
  const n = Object.keys(expected).length;
  for (const name of Object.keys(expected)) await expect(page.getByTestId(`bin-clip-${name}`)).toBeVisible();
  await expect(page.getByTestId("media-summary")).toContainText(`${n} clips`);
  // Metadata and audio analysis run in the background; the import view counts them (no time estimates).
  await expect(page.getByTestId("import-progress")).toContainText(`metadata ${n} / ${n}`);
  await expect(page.getByTestId("import-progress")).toContainText(`audio ${n - 1} / ${n - 1}`); // all but the drone
  await shot("02-imported");
});

test("finds clips with the search grammar and bins", async () => {
  const search = page.getByTestId("media-search");
  await search.fill("audio");
  await expect(page.getByTestId("media-summary")).toContainText("1 clips");
  await expect(page.getByTestId("bin-clip-230614_001.WAV")).toBeVisible();
  await search.fill("A00");
  await expect(page.getByTestId("media-summary")).toContainText("2 clips");
  await search.fill("");
  await page.getByRole("button", { name: "List" }).click();
  await expect(page.getByTestId("media-list")).toBeVisible();
  await page.getByRole("button", { name: "Grid" }).click();
});

test("explains the timeline before the first synchronisation", async () => {
  // A new project's timeline used to open blank (and count 0 clips) until the project was reopened.
  const n = Object.keys(expected).length;
  await page.getByTestId("stage-timeline").click();
  const empty = page.getByTestId("timeline-empty");
  await expect(empty).toContainText("Not synchronised yet");
  await expect(empty).toContainText(`${n} clips are ready to line up`);
  await expect(page.getByTestId("timeline-sync")).toBeVisible();
  await expect(page.getByTestId("statusbar-clips")).toHaveText(`${n} clips`);
  await expect(page.getByTestId("review-queue")).toHaveCount(0); // nothing to review before a sync
  await page.getByTestId("stage-media").click();
});

test("synchronises every clip to its true position", async () => {
  // Settings → Synchronization: the cameras' timecode was jam-synced on this shoot.
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-synchronization").click();
  await page.getByRole("switch", { name: "Timecode is jam-synced" }).click();
  await expect(page.getByRole("switch", { name: "Timecode is jam-synced" })).toHaveAttribute("aria-checked", "true");
  await page.getByRole("button", { name: "Close settings" }).click();

  await page.getByTestId("sync").click();
  await expect(page.getByTestId("sync-screen")).toBeVisible();
  await expect(page.getByTestId("results")).toBeVisible({ timeout: 180_000 });
  const n = Object.keys(expected).length;
  await expect(page.getByTestId("sync-stats")).toContainText(`${n} synced`);
  await shot("03-results");

  await page.getByTestId("open-timeline").click();
  const origin = await startOf("230614_001.WAV");
  for (const [name, offset] of Object.entries(expected)) {
    const clip = page.getByTestId(`clip-${name}`);
    await expect(clip).toHaveAttribute("data-status", "synced");
    expect(Math.abs((await startOf(name)) - origin - offset), name).toBeLessThan(0.04);
  }
  // Waveforms are drawn from the engine's cache, for every clip with audio (all but the drone).
  await expect
    .poll(() => page.evaluate(() => window.mcsyncTimeline?.stats() ?? null))
    .toEqual({ clips: n, waveforms: n - 1 });
  await shot("04-timeline");
});

test("explains a clip in the inspector", async () => {
  await clickClip("A002.MOV");
  const inspector = page.getByTestId("inspector");
  await expect(inspector).toContainText("A002.MOV");
  await expect(page.getByTestId("inspector-status")).toHaveText("Synced");
  await expect(page.getByTestId("matches")).toContainText("230614_001.WAV");
});

test("drags a clip by hand and undoes it", async () => {
  const before = await startOf("C0001.MP4");
  const clip = page.getByTestId("clip-C0001.MP4");
  const box = await clipBox("C0001.MP4");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2, { steps: 6 });
  await page.mouse.move(box.x + box.width / 2 + 120, box.y + box.height / 2, { steps: 6 });
  await page.mouse.up();

  await expect.poll(() => startOf("C0001.MP4")).toBeGreaterThan(before + 10);
  await expect(clip).toHaveClass(/manual/);
  await expect(page.getByTestId("inspector")).toContainText("Placed by hand.");
  await shot("05-dragged");

  await page.getByRole("button", { name: "Undo" }).click();
  await expect.poll(() => startOf("C0001.MP4")).toBeCloseTo(before, 3);
  await expect(clip).not.toHaveClass(/manual/);
});

test("nudges a clip off and snaps it back to the audio", async () => {
  const before = await startOf("A002.MOV");
  // Select from the keyboard, through the timeline's accessible clip list.
  await page.getByTestId("clip-A002.MOV").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("inspector")).toContainText("A002.MOV");
  for (let i = 0; i < 3; i++) await page.keyboard.press("Shift+ArrowRight"); // 30 frames ≈ 1.25 s
  await expect.poll(() => startOf("A002.MOV")).toBeGreaterThan(before + 1);

  await page.getByTestId("snap").click();
  await expect(page.getByTestId("toast-success").last()).toContainText("Snapped to the audio");
  await expect.poll(async () => Math.abs((await startOf("A002.MOV")) - before)).toBeLessThan(0.002);
});

test("rejects a wrong match from the inspector and restores it", async () => {
  await clickClip("A002.MOV");
  await page.getByTestId("reject-230614_001.WAV").click();
  await expect(page.getByTestId("matches").getByRole("button", { name: "Restore" })).toBeVisible();
  await page.getByTestId("matches").getByRole("button", { name: "Restore" }).click();
  await expect(page.getByTestId("reject-230614_001.WAV")).toBeVisible();
});

test("exports the timeline for Premiere Pro and Resolve", async () => {
  for (const [format, file] of [
    ["xmeml", "Smith.xml"],
    ["fcpxml", "Smith.fcpxml"],
  ] as const) {
    const out = path.join(work, file);
    await app.evaluate(({ dialog }, target) => {
      dialog.showSaveDialog = (async () => ({ canceled: false, filePath: target })) as typeof dialog.showSaveDialog;
    }, out);
    await page.getByTestId("stage-export").click();
    await page.getByTestId(`format-${format}`).check();
    await page.getByTestId("rate").selectOption("25");
    await page.getByTestId("export-submit").click();
    const result = page.getByTestId("export-result");
    await expect(result).toContainText(`Exported ${Object.keys(expected).length} clips to ${file}`);
    await expect(result).toContainText("25 fps");
    // The recorder starts the timeline here, so it is sample-accurate in both formats.
    await expect(page.getByTestId("export-accuracy")).toContainText("Recorder audio is placed to the sample");
    if (format === "xmeml") await shot("06-exported");
    await result.getByRole("button", { name: "Done" }).click();
    await expect(page.getByTestId("export-dialog")).toBeHidden();

    const xml = fs.readFileSync(out, "utf-8");
    for (const name of Object.keys(expected)) expect(xml).toContain(name);
    expect(xml).toContain(format === "xmeml" ? '<xmeml version="5">' : '<fcpxml version="1.10">');
  }
});

test("reopens the project with its timeline", async () => {
  const start = await startOf("A002.MOV");
  await closeApp(app, path.join(work, "user-data"));
  await launch();
  await expect(page.getByTestId("welcome")).toContainText("Smith");
  await page.getByRole("button", { name: "Smith" }).click();
  await expect(page.getByTestId("media-screen")).toBeVisible();
  await page.getByTestId("stage-timeline").click();
  await expect(page.getByTestId("clip-A002.MOV")).toBeAttached();
  expect(await startOf("A002.MOV")).toBeCloseTo(start, 6);
  await shot("07-reopened");
});
