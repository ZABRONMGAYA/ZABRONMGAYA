// A multi-session production (hundreds of files, with damaged files, duplicates and silent clips) through the real
// app: background import with live counts, pause and resume, quitting mid-analysis and resuming on reopen, the
// windowed media browser, search, bulk actions, sync results, errors, duplicates, offline media and relinking.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { type ElectronApplication, type Page, _electron as electron, expect, test } from "@playwright/test";

import { closeApp, printEngineLog } from "./helpers";

const appDir = path.resolve(import.meta.dirname, "..");
const python = process.env.MCSYNC_PYTHON ?? (process.platform === "win32" ? "python" : "python3");
const screens = path.join(appDir, "test-results", "screens");
const packagedApp = process.env.MCSYNC_E2E_APP;

// 2 sessions × 4 cameras × 30 clips + 2 × 2 recorder files, plus 25 problem clips, 5 damaged files, 5 copies.
const GENERATE = `
import json, sys
from pathlib import Path
from mcsync.testing.production import ProductionPlan, generate_production
plan = ProductionPlan(sessions=2, cameras=4, clips_per_camera=30, recorders=1, files_per_recorder=2,
                      recorder_file_s=300.0, clip_s=(8.0, 16.0), seed=5)
prod = generate_production(Path(sys.argv[1]), plan, workers=4)
counts = {}
for f in prod.files.values():
    counts[f.expect] = counts.get(f.expect, 0) + 1
print(json.dumps({"counts": counts, "sync": plan.video_files + plan.audio_files}))
`;

let work: string;
let root: string;
let truth: { counts: Record<string, number>; sync: number };
let app: ElectronApplication;
let page: Page;

test.describe.configure({ mode: "serial" });

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
  page = await app.firstWindow();
  await page.setViewportSize({ width: 1440, height: 900 });
}

async function answerDialogs(answers: { save?: string; open?: string[] }): Promise<void> {
  await app.evaluate(({ dialog }, { save, open }) => {
    dialog.showSaveDialog = (async () => ({ canceled: !save, filePath: save ?? "" })) as typeof dialog.showSaveDialog;
    dialog.showOpenDialog = (async () => ({ canceled: !open, filePaths: open ?? [] })) as typeof dialog.showOpenDialog;
  }, answers);
}

async function shot(name: string): Promise<void> {
  await page.screenshot({ path: path.join(screens, `production-${name}.png`) });
}

/** Clips counted in the media summary ("123 clips"). */
async function shownClips(): Promise<number> {
  const text = (await page.getByTestId("media-summary").textContent()) ?? "";
  return Number(/([\d,]+) clips/.exec(text)?.[1]?.replace(/,/g, "") ?? -1);
}

const totalClips = () =>
  truth.sync +
  truth.counts["silent"]! +
  truth.counts["no-audio"]! +
  truth.counts["unrelated"]! +
  truth.counts["duplicate"]!;

test.beforeAll(() => {
  work = fs.mkdtempSync(path.join(os.tmpdir(), "syncora-production-"));
  root = path.join(work, "FESTIVAL");
  truth = JSON.parse(execFileSync(python, ["-c", GENERATE, root], { encoding: "utf-8" })) as typeof truth;
  fs.mkdirSync(screens, { recursive: true });
});

test.afterEach(async ({}, testInfo) => {
  if (testInfo.status !== testInfo.expectedStatus && page) {
    printEngineLog(path.join(work, "user-data"), `"${testInfo.title}" failed`);
    await page.screenshot({ path: testInfo.outputPath("failure.png") }).catch(() => undefined);
  }
});

test.afterAll(async () => {
  if (app) await closeApp(app, path.join(work, "user-data")).catch(() => undefined);
  fs.rmSync(work, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 });
});

test("imports hundreds of files in the background and can pause", async () => {
  await launch();
  await expect(page.getByTestId("new-project")).toBeEnabled();
  await answerDialogs({ save: path.join(work, "Festival.syncora"), open: [root] });
  await page.getByTestId("new-project").click();
  await page.getByTestId("import-folder").click();
  // Files are counted as they are found, and appear in the browser while the rest is processed.
  await expect(page.getByTestId("import-progress")).toContainText(/Found \d+ files/);
  await expect.poll(shownClips, { timeout: 120_000 }).toBeGreaterThan(20);

  await page.getByTestId("stage-sync").click();
  await page.getByTestId("pause").click();
  await expect(page.getByTestId("analysis-title")).toHaveText("Paused");
  await expect(page.getByTestId("resume")).toBeVisible();
  await shot("paused");
  await page.getByTestId("resume").click();
  await expect(page.getByTestId("pause")).toBeVisible();
});

test("quits in the middle of the analysis and resumes on reopen", async () => {
  // Pause, queue a fresh analysis of every clip (it cannot start while paused), and quit with that work queued.
  await page.getByTestId("pause").click();
  await expect(page.getByTestId("analysis-title")).toHaveText("Paused");
  await page.getByTestId("stage-media").click();
  await page.locator("[data-testid^='bin-clip-']").first().click();
  await page.keyboard.press("ControlOrMeta+a");
  await expect(page.getByTestId("selection-bar")).toContainText("selected");
  await page.getByTestId("bulk-analyze").click();
  await expect(page.getByTestId("toast-info").last()).toContainText("Queued");
  await closeApp(app, path.join(work, "user-data"));
  await launch();
  await page.getByRole("button", { name: "Festival" }).click();
  await expect(page.getByTestId("resume-dialog")).toBeVisible();
  await expect(page.getByTestId("resume-dialog")).toContainText("Previous analysis found");
  await shot("resume");
  await page.getByTestId("resume-work").click();
  await expect(page.getByTestId("resume-dialog")).toBeHidden();
  await page.getByTestId("stage-sync").click();
  await expect(page.getByTestId("stage-row-analyze")).toContainText("Complete", { timeout: 300_000 });
  await expect(page.getByTestId("stage-row-probe")).toContainText(`${truth.counts["damaged"]} failed`);
});

test("lists the errors and keeps them failed when retried", async () => {
  await page.getByTestId("view-errors").click();
  const dialog = page.getByTestId("errors-dialog");
  await expect(dialog).toContainText("D0001.MP4");
  await dialog.getByRole("button", { name: "Close" }).click();
  await page.getByTestId("retry-failed").click();
  await expect(page.getByTestId("stage-row-probe")).toContainText(`${truth.counts["damaged"]} failed`, {
    timeout: 60_000,
  });
});

test("renders only what is on screen and finds clips by search", async () => {
  await page.getByTestId("stage-media").click();
  await expect.poll(shownClips).toBe(totalClips());
  // Windowed rendering: a few dozen cards in the page, not hundreds.
  const cards = await page.locator("[data-testid^='bin-clip-']").count();
  expect(cards).toBeLessThan(80);
  await page.getByTestId("media-search").fill("S00_CAMB");
  await expect.poll(shownClips).toBe(30);
  await page.getByTestId("media-search").fill("audio");
  await expect.poll(shownClips).toBe(4);
  await page.getByTestId("media-search").fill("");
  await page.getByTestId("bin-duplicates").click();
  await expect.poll(shownClips).toBe(truth.counts["duplicate"]!);
  await page.getByTestId("bin-all").click();
});

test("synchronises the production and sorts the results", async () => {
  await page.getByTestId("sync").click();
  await expect(page.getByTestId("results")).toBeVisible({ timeout: 600_000 });
  await expect(page.getByTestId("category-failed")).toContainText(String(truth.counts["damaged"]));
  await expect(page.getByTestId("category-skipped")).toContainText(String(truth.counts["duplicate"]));
  const synced = Number(
    (await page.getByTestId("category-synchronized").locator(".sy-category__n").textContent())!.replace(/,/g, ""),
  );
  expect(synced).toBeGreaterThanOrEqual(truth.sync - truth.counts["duplicate"]!);
  await shot("results");
  // Unsynchronized and review clips open in the media browser, filtered.
  await page.getByTestId("category-review").click();
  await expect(page.getByTestId("media-search")).toHaveValue("review");
  expect(await shownClips()).toBeGreaterThan(0);
  await page.getByTestId("media-search").fill("");
});

test("keeps both copies of a duplicate when asked", async () => {
  await page
    .getByTestId("duplicates-banner")
    .getByRole("button", { name: /Review duplicates/ })
    .click();
  const dialog = page.getByTestId("duplicates-dialog");
  await dialog.getByRole("button", { name: "Keep both" }).first().click();
  await expect(dialog).toContainText("Kept");
  await dialog.getByRole("button", { name: "Done" }).click();
});

test("removes clips from the project without touching the files", async () => {
  await page.getByTestId("media-search").fill("S01_CAMD C0030");
  await expect.poll(shownClips).toBe(1);
  const card = page.locator("[data-testid='bin-clip-C0030.MP4']");
  await card.click();
  await expect(page.getByTestId("selection-bar")).toContainText("1 selected");
  await page.getByTestId("bulk-remove").click();
  await page.getByTestId("confirm-remove").click();
  await expect.poll(shownClips).toBe(0);
  expect(fs.existsSync(path.join(root, "CARDS", "S01_CAMD", "DCIM", "C0030.MP4"))).toBe(true);
  await page.getByTestId("media-search").fill("");
});

test("shows offline media and relinks a moved card", async () => {
  await closeApp(app, path.join(work, "user-data"));
  const card = path.join(root, "CARDS", "S00_CAMB");
  const moved = path.join(work, "RELOCATED", "S00_CAMB");
  fs.mkdirSync(path.dirname(moved), { recursive: true });
  fs.renameSync(card, moved);
  await launch();
  await page.getByRole("button", { name: "Festival" }).click();
  const banner = page.getByTestId("offline-banner");
  await expect(banner).toContainText("30 clips offline");
  await shot("offline");
  await answerDialogs({ open: [path.join(work, "RELOCATED")] });
  await banner.getByRole("button", { name: "Find folder…" }).click();
  await expect(page.getByTestId("toast-success").last()).toContainText("Relinked 30 clip(s)");
  await expect(page.getByTestId("offline-banner")).toBeHidden();
});
