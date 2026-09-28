// Acceptance test of the multicamera preview (Sync workspace, S06) in the real app with the real engine: four
// cameras (a noisy gimbal, a ProRes camera Chromium cannot play, a phone with a clip without sound) and two external
// recorders, all cut from one scene at known offsets (mcsync.testing.multicam --scenario acceptance). Every picture
// carries its scene frame number, so the test reads back what each camera window shows and checks they agree.
//
// Checks: video plays · cameras are in sync · scrubbing · timecode · audio monitor · camera selection · program
// view · inspector · manual offsets · timeline playhead · review workflow · evidence · sync points.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { type ElectronApplication, type Page, _electron as electron, expect, test } from "@playwright/test";

import { closeApp, mainWindow, printEngineLog } from "./helpers";

const appDir = path.resolve(import.meta.dirname, "..");
const python = process.env.MCSYNC_PYTHON ?? (process.platform === "win32" ? "python" : "python3");
const screens = path.join(appDir, "test-results", "screens");
const packagedApp = process.env.MCSYNC_E2E_APP;

interface Truth {
  device: string;
  start: number;
  duration: number;
  has_audio: boolean;
}

let work: string;
let media: string;
let truth: Record<string, Truth>;
let app: ElectronApplication;
let page: Page;

test.describe.configure({ mode: "serial" });

async function shot(name: string): Promise<void> {
  await page.screenshot({ path: path.join(screens, `${name}.png`) });
}

/** The scene frame each camera window shows (null: no picture), read from the 16-bit pattern atop every frame. */
async function sceneFrames(): Promise<Record<string, number | null>> {
  return page.evaluate(() => {
    const out: Record<string, number | null> = {};
    const probe = document.createElement("canvas");
    probe.width = 320;
    probe.height = 180;
    const ctx = probe.getContext("2d", { willReadFrequently: true })!;
    for (const tile of Array.from(document.querySelectorAll<HTMLElement>("[data-testid^=angle-]"))) {
      const name = tile.dataset.testid!.slice("angle-".length);
      if (tile.dataset.view !== "media") {
        out[name] = null;
        continue;
      }
      const video = tile.querySelector("video")!;
      const canvas = tile.querySelector("canvas")!;
      const source = video.style.visibility !== "hidden" && video.videoWidth ? video : canvas.width > 0 ? canvas : null;
      if (!source) {
        out[name] = null;
        continue;
      }
      ctx.clearRect(0, 0, 320, 180);
      ctx.drawImage(source, 0, 0, 320, 180);
      let n = 0;
      for (let k = 0; k < 16; k++) {
        const px = ctx.getImageData(Math.floor((k + 0.5) * 20), 20, 1, 1).data;
        if (px[0]! > 128) n |= 1 << k;
      }
      out[name] = n === 0 ? null : n; // black: no picture yet (scene frame 0 is never asked for)
    }
    return out;
  });
}

/** Every camera window with a picture shows the same scene frame (within `tolerance` frames). */
async function expectInSync(tolerance: number, minTiles = 2): Promise<Record<string, number | null>> {
  let last: Record<string, number | null> = {};
  await expect
    .poll(
      async () => {
        last = await sceneFrames();
        const values = Object.values(last).filter((v): v is number => v !== null);
        if (values.length < minTiles) return `only ${values.length} pictures: ${JSON.stringify(last)}`;
        const spread = Math.max(...values) - Math.min(...values);
        return spread <= tolerance ? "in sync" : `spread ${spread} frames: ${JSON.stringify(last)}`;
      },
      { timeout: 20_000, intervals: [200, 300, 500] },
    )
    .toBe("in sync");
  return last;
}

async function clockNow(): Promise<number> {
  return page.evaluate(() => window.mcsyncMulticam!.clock.now());
}

async function seek(t: number): Promise<void> {
  await page.evaluate((x) => {
    window.mcsyncMulticam!.clock.pause();
    window.mcsyncMulticam!.clock.seek(x);
  }, t);
}

/** media.index rows keyed by path relative to the media folder. */
async function placements(): Promise<Record<string, { start: number | null; group: number | null; category: string }>> {
  const index = await page.evaluate(async () => {
    const bridge = (
      window as unknown as {
        mcsync: {
          invoke(m: string, p: object): Promise<{ ok: boolean; result?: { columns: string[]; rows: unknown[][] } }>;
        };
      }
    ).mcsync;
    const r = await bridge.invoke("media.index", {});
    return r.ok ? r.result : null;
  });
  expect(index).not.toBeNull();
  const cols = Object.fromEntries(index!.columns.map((c: string, k: number) => [c, k]));
  const out: Record<string, { start: number | null; group: number | null; category: string }> = {};
  for (const row of index!.rows) {
    const rel = path
      .relative(media, row[cols.path!] as string)
      .split(path.sep)
      .join("/");
    out[rel] = {
      start: row[cols.start_s!] as number | null,
      group: row[cols.group!] as number | null,
      category: row[cols.category!] as string,
    };
  }
  return out;
}

/** Where each clip starts on the timeline (the master clock's time), keyed by path relative to the media folder. */
async function timelineStarts(): Promise<Record<string, number>> {
  const timeline = await page.evaluate(async () => {
    const bridge = (
      window as unknown as {
        mcsync: {
          invoke(
            m: string,
            p: object,
          ): Promise<{ ok: boolean; result?: { groups: { clips: { path: string; start_s: number | null }[] }[] } }>;
        };
      }
    ).mcsync;
    const r = await bridge.invoke("timeline.get", {});
    return r.ok ? r.result : null;
  });
  expect(timeline).not.toBeNull();
  const out: Record<string, number> = {};
  for (const g of timeline!.groups)
    for (const c of g.clips)
      if (c.start_s !== null) out[path.relative(media, c.path).split(path.sep).join("/")] = c.start_s;
  return out;
}

const ZOOM = "SOUND/ZOOM/ZOOM_001.WAV";

/** Master time of a scene time (through the recorder, which covers the whole scene). */
async function masterOfScene(sceneT: number): Promise<number> {
  return sceneT - truth[ZOOM]!.start + (await timelineStarts())[ZOOM]!;
}

/** The camera window of a device (the engine names devices by make and folder, e.g. "Sony (FX3)"). */
function tileOf(device: string) {
  return page.locator(`[data-testid^="angle-"][data-testid*="${device}"]`);
}

function frameOf(frames: Record<string, number | null>, device: string): number | null {
  const key = Object.keys(frames).find((k) => k.includes(device));
  return key === undefined ? null : frames[key]!;
}

/** A master time where the most cameras record at once (scene time → master time through the reference). */
function busiestSceneTime(): number {
  let best = { t: 0, n: -1 };
  for (let t = 5; t < 320; t += 1) {
    const n = Object.values(truth)
      .filter((c) => c.device !== "ZOOM" && c.device !== "LAV")
      .filter((c) => t >= c.start + 1 && t <= c.start + c.duration - 2).length;
    if (n > best.n) best = { t, n };
  }
  return best.t;
}

test.beforeAll(() => {
  work = fs.mkdtempSync(path.join(os.tmpdir(), "syncora-multicam-"));
  execFileSync(python, ["-m", "mcsync.testing.multicam", work, "--scenario", "acceptance", "--generate-only"], {
    stdio: "inherit",
  });
  media = path.join(work, "media");
  truth = JSON.parse(fs.readFileSync(path.join(media, "truth.json"), "utf-8")) as Record<string, Truth>;
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

test("imports four cameras and two recorders and synchronises them to their known offsets", async () => {
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
  await expect(page.getByTestId("new-project")).toBeEnabled();
  await app.evaluate(
    ({ dialog }, { save, open }) => {
      dialog.showSaveDialog = (async () => ({ canceled: false, filePath: save })) as typeof dialog.showSaveDialog;
      dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: open })) as typeof dialog.showOpenDialog;
    },
    { save: path.join(work, "Acceptance.syncora"), open: [media] },
  );
  await page.getByTestId("new-project").click();
  await page.getByTestId("import-folder").click();
  const n = Object.keys(truth).length;
  await expect(page.getByTestId("media-summary")).toContainText(`${n} clips`, { timeout: 60_000 });

  if (process.platform === "win32") {
    // Windows allows at most 61 processes in a pool: asking for 64 matchers used to fail the whole sync with
    // "max_workers must be <= 61". The engine now caps the request, and the sync below runs with that pool.
    const configured = await page.evaluate(async () => {
      const bridge = (
        window as unknown as {
          mcsync: { invoke(m: string, p: object): Promise<{ ok: boolean; result?: { plan: { match: number } } }> };
        }
      ).mcsync;
      return bridge.invoke("engine.configure", { workers: { match: 64 } });
    });
    expect(configured.result?.plan.match).toBe(61);
  }
  await page.getByTestId("sync").click();
  await expect(page.getByTestId("results")).toBeVisible({ timeout: 300_000 });
  await shot("mc-01-results");

  // Known offsets: every clip with sound where it belongs (within a frame), relative to the recorder.
  const placed = await placements();
  const ref = placed["SOUND/ZOOM/ZOOM_001.WAV"]!;
  const refTruth = truth["SOUND/ZOOM/ZOOM_001.WAV"]!;
  for (const [rel, t] of Object.entries(truth)) {
    const p = placed[rel]!;
    if (!t.has_audio) {
      // No sound: never reported as synchronised by audio.
      expect(["review", "manual"], rel).toContain(p.category);
      continue;
    }
    expect(p.start, rel).not.toBeNull();
    expect(p.group, rel).toBe(ref.group);
    expect(Math.abs(p.start! - ref.start! - (t.start - refTruth.start)), rel).toBeLessThan(0.04);
    expect(["synchronized", "confirmed", "high_confidence"], rel).toContain(p.category);
  }
});

test("1-3 · plays every camera in sync, the ProRes camera through FFmpeg", async () => {
  await page.getByTestId("sync-view-workspace").click();
  const viewer = page.getByTestId("multicam-viewer");
  await expect(viewer).toBeVisible();
  await expect(page.locator("[data-testid^=angle-]")).toHaveCount(4); // the cameras (recorders are heard, not shown)

  const sceneT = busiestSceneTime();
  const master = await masterOfScene(sceneT);
  await seek(master);
  const frames = await expectInSync(1, 3);
  // …and at the right moment of the scene.
  const expectedFrame = Math.round(sceneT * 25);
  for (const [name, f] of Object.entries(frames))
    if (f !== null) expect(Math.abs(f - expectedFrame), name).toBeLessThanOrEqual(1);
  const tiles = await page.evaluate(() => window.mcsyncMulticam!.tiles());
  expect(Object.values(tiles).some((s) => s.mode === "frames")).toBe(true); // ProRes: FFmpeg pictures
  expect(Object.values(tiles).some((s) => s.mode === "native")).toBe(true); // H.264: Chromium
  await shot("mc-02-grid-paused");

  // Play two seconds: the clock advances, pictures follow, and stay together.
  const tc0 = await page.getByTestId("master-timecode").textContent();
  await page.getByTestId("play").click();
  await page.waitForTimeout(2500);
  const moving = await sceneFrames();
  await page.getByTestId("play").click();
  const t1 = await clockNow();
  expect(t1 - master).toBeGreaterThan(1.5);
  const values = Object.values(moving).filter((v): v is number => v !== null);
  expect(Math.max(...values) - Math.min(...values), JSON.stringify(moving)).toBeLessThanOrEqual(4);
  expect(Math.min(...values)).toBeGreaterThan(expectedFrame + 25);
  await expectInSync(1, 3);
  expect(await page.getByTestId("master-timecode").textContent()).not.toBe(tc0); // 4: the timecode follows

  // Scrub: drag along the ruler; every camera shows the picture under the pointer.
  const ruler = page.locator(".ruler-row");
  const box = (await ruler.boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.3, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.45, box.y + box.height / 2, { steps: 8 });
  await page.mouse.up();
  const scrubbed = await clockNow();
  expect(Math.abs(scrubbed - t1)).toBeGreaterThan(1);
  await shot("mc-03-scrubbed");
});

test("5 · hears one source at a time, the one chosen", async () => {
  const select = page.getByTestId("monitor-select");
  await expect(select).toBeVisible();
  const lav = await select.locator("option", { hasText: "LAV" }).getAttribute("value");
  await select.selectOption(lav!);
  await seek(await masterOfScene(truth["SOUND/LAV/LAV_001.WAV"]!.start + 10));
  await page.getByTestId("play").click();
  await expect
    .poll(async () => page.evaluate(() => window.mcsyncMulticam!.monitor()), { timeout: 10_000 })
    .toMatchObject({ path: expect.stringContaining("LAV_001.WAV") });
  await page.getByTestId("play").click();
  expect((await page.evaluate(() => window.mcsyncMulticam!.monitor())).chunks).toBeGreaterThan(0);
});

test("6-8 · selects a camera, shows it large, and explains it in the inspector", async () => {
  await seek(await masterOfScene(busiestSceneTime()));
  const tile = page.locator("[data-testid^=angle-][data-view=media]").first();
  await tile.click();
  await expect(tile).toHaveClass(/mc-tile--active/);
  const inspector = page.getByTestId("inspector");
  await expect(page.getByTestId("inspector-status")).toHaveText(/CONFIRMED|HIGH CONFIDENCE|SYNCHRONIZED|REVIEW/);
  await expect(page.getByTestId("inspector-offset")).toHaveText(/^[+−]\d/);
  await expect(inspector).toContainText("Method");
  await expect(page.getByTestId("inspector-preview")).toHaveText(/natively|FFmpeg/);

  await page.keyboard.press("2");
  await expect(page.locator(".mc-tile--active")).toHaveCount(1);
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("viewer-program")).toBeVisible();
  await shot("mc-04-program");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("viewer-grid")).toBeVisible();
});

test("9-10 · a manual offset moves the picture and the timeline, and undoes", async () => {
  // The FX3's clip under the playhead: nudge it one frame later.
  const fx3 = tileOf("FX3");
  await expect(fx3).toHaveAttribute("data-view", "media");
  await fx3.click();
  const before = frameOf(await sceneFrames(), "FX3")!;
  const offset0 = await page.getByTestId("inspector-offset").textContent();
  await page.keyboard.press(".");
  await expect(page.getByTestId("inspector-offset")).not.toHaveText(offset0!);
  // One frame later on the timeline: at the same master time the camera shows the frame before.
  await expect.poll(async () => frameOf(await sceneFrames(), "FX3"), { timeout: 10_000 }).toBe(before - 1);
  await expect(page.getByTestId("inspector-status")).toHaveText("CONFIRMED"); // placed by hand
  await shot("mc-05-nudged");
  // ⌘Z / Ctrl+Z is a menu accelerator (synthetic key presses do not reach the native menu): the Undo button.
  await page.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByTestId("inspector-offset")).toHaveText(offset0!, { timeout: 10_000 });
  await expect.poll(async () => frameOf(await sceneFrames(), "FX3"), { timeout: 10_000 }).toBe(before);

  // 10: the playhead on the timeline follows playback.
  const playhead = page.getByTestId("playhead");
  const x0 = (await playhead.boundingBox())!.x;
  await page.getByTestId("play").click();
  await page.waitForTimeout(1500);
  await page.getByTestId("play").click();
  expect((await playhead.boundingBox())!.x).toBeGreaterThan(x0);
});

test("13-14 · sync points and the evidence behind a placement", async () => {
  const gimbal = tileOf("GIMBAL");
  // A moment the gimbal records.
  const [rel] = Object.entries(truth).find(([, t]) => t.device === "GIMBAL" && t.has_audio)!;
  const start = (await timelineStarts())[rel]!;
  await seek(start + 3);
  await expect(gimbal).toHaveAttribute("data-view", "media");
  await gimbal.click();
  await page.getByTestId("show-evidence").click();
  await expect(page.getByTestId("matches")).toContainText(/\d+\/\d+ win/);
  await page.keyboard.press("s");
  await expect(page.getByTestId("sync-points")).toContainText("One point");
  await seek(start + 8);
  await page.keyboard.press("s");
  await expect(page.getByTestId("sync-points")).toContainText("2 points agree");
  await shot("mc-06-evidence-points");
});

test("11-12 · reviews the clips that need it, beside the reference", async () => {
  const start = page.getByTestId("start-review");
  await page.keyboard.press("Escape");
  if ((await start.count()) === 0) {
    test.info().annotations.push({ type: "note", description: "nothing to review in this run" });
    return;
  }
  await start.click();
  const bar = page.getByTestId("review-bar");
  await expect(bar).toBeVisible();
  // The clip under review on the right, a synchronised camera overlapping it on the left.
  await expect(page.getByTestId("viewer-compare").locator("[data-testid^=angle-]")).toHaveCount(2);
  await shot("mc-07-review");
  const total = Number(/REVIEW \d+ \/ (\d+)/.exec((await bar.textContent()) ?? "")![1]);
  const reviewed = await page.getByTestId("inspector").locator(".sy-si__facts span").nth(1).textContent();
  // Accept: the clip is locked where it is (confirmed) and the next one comes up; the last one ends the review.
  await page.getByTestId("review-accept").click();
  if (total === 1) {
    await expect(bar).toHaveCount(0);
    await expect(page.getByTestId("inspector-status")).toHaveText("CONFIRMED"); // accepted: locked by the user
  } else {
    await expect(bar).toContainText(`REVIEW 2 / ${total}`);
    await page.getByRole("button", { name: "Close the review" }).click();
    await expect(bar).toHaveCount(0);
  }
  expect(reviewed).toBeTruthy();
});
