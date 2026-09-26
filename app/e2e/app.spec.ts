// The whole editor workflow in the real app: new project → import a card dump → synchronise → review →
// drag a clip → undo → nudge and snap back to the audio → reopen the project.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { type ElectronApplication, type Page, _electron as electron, expect, test } from "@playwright/test";

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

async function launch(): Promise<void> {
  app = await electron.launch({
    args: [appDir],
    env: {
      ...process.env,
      MCSYNC_NO_SANDBOX: "1",
      MCSYNC_CACHE_DIR: path.join(work, "cache"),
      MCSYNC_USER_DATA: path.join(work, "user-data"),
    },
  });
  page = await app.firstWindow();
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
  work = fs.mkdtempSync(path.join(os.tmpdir(), "mcsync-e2e-"));
  shootDir = path.join(work, "Smith wedding");
  expected = JSON.parse(execFileSync(python, ["-c", GENERATE, shootDir], { encoding: "utf-8" })) as Record<
    string,
    number
  >;
  fs.mkdirSync(screens, { recursive: true });
});

test.afterAll(async () => {
  await app?.close();
  // Windows may hold the project file open for a moment after the engine exits.
  fs.rmSync(work, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 });
});

test("starts on the welcome screen with the engine ready", async () => {
  await launch();
  await expect(page.getByTestId("welcome")).toBeVisible();
  await expect(page.getByTestId("new-project")).toBeEnabled();
  await expect(page.getByTestId("statusbar")).toContainText("FFmpeg");
  await shot("01-welcome");
});

test("creates a project and imports a folder of footage", async () => {
  await answerDialogs({ save: path.join(work, "Smith.mcsync"), open: [shootDir] });
  await page.getByTestId("new-project").click();
  await expect(page.getByTestId("toolbar")).toContainText("Smith");

  await page.getByTestId("import-folder").click();
  await expect(page.getByTestId("toast-success")).toContainText(`Imported ${Object.keys(expected).length} file(s)`);
  for (const name of Object.keys(expected)) await expect(page.getByTestId(`bin-clip-${name}`)).toBeVisible();
  await shot("02-imported");
});

test("synchronises every clip to its true position", async () => {
  await page.getByTestId("jam-synced").check();
  await page.getByTestId("sync").click();
  await expect(page.getByTestId("sync-stats")).toContainText(`${Object.keys(expected).length} synced`, {
    timeout: 180_000,
  });

  const origin = await startOf("230614_001.WAV");
  for (const [name, offset] of Object.entries(expected)) {
    const clip = page.getByTestId(`clip-${name}`);
    await expect(clip).toHaveAttribute("data-status", "synced");
    expect(Math.abs((await startOf(name)) - origin - offset), name).toBeLessThan(0.04);
  }
  await expect(page.getByTestId("clip-230614_001.WAV")).toContainText("(reference)");
  // Waveforms are drawn from the engine's cache, for every clip with audio (all but the drone).
  await expect
    .poll(() => page.evaluate(() => window.mcsyncTimeline?.stats() ?? null))
    .toEqual({ clips: Object.keys(expected).length, waveforms: Object.keys(expected).length - 1 });
  await shot("03-synced");
});

test("explains a clip in the inspector", async () => {
  await clickClip("A002.MOV");
  const inspector = page.getByTestId("inspector");
  await expect(inspector).toContainText("A002.MOV");
  await expect(page.getByTestId("inspector-status")).toHaveText("Synced");
  await expect(page.getByTestId("matches")).toContainText("230614_001.WAV");
  await shot("04-inspector");
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
  await shot("06-snapped");
});

test("rejects a wrong match from the inspector and restores it", async () => {
  await clickClip("A002.MOV");
  await page.getByTestId("reject-230614_001.WAV").click();
  await expect(page.getByTestId("matches").getByRole("button", { name: "Restore" })).toBeVisible();
  await page.getByTestId("matches").getByRole("button", { name: "Restore" }).click();
  await expect(page.getByTestId("reject-230614_001.WAV")).toBeVisible();
});

test("reopens the project with its timeline", async () => {
  const start = await startOf("A002.MOV");
  await app.close();
  await launch();
  await expect(page.getByTestId("welcome")).toContainText("Smith.mcsync");
  await page.getByRole("button", { name: "Smith.mcsync" }).click();
  await expect(page.getByTestId("clip-A002.MOV")).toBeAttached();
  expect(await startOf("A002.MOV")).toBeCloseTo(start, 6);
  await shot("07-reopened");
});
