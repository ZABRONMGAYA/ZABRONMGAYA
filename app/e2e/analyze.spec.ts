// Stage 03 Analyze and S07 AI sync in the real app, with the real speech models: a recorder and a camera of the same
// toast are transcribed, a speaker renamed, a sentence searched and marked, the recording played through the media
// protocol, and the camera placed by AI sync from what was said.
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
const dialog = path.resolve(appDir, "..", "engine", "tests", "data", "dialog.ogg");
const devModels = path.resolve(appDir, "..", "engine", "models");
const OFFSET = 5.3; // the camera started this much later than the recorder

// The speech models ship inside the packaged app; from source they come from engine/scripts/fetch_models.py.
const haveModels =
  Boolean(packagedApp) ||
  Boolean(process.env.MCSYNC_MODELS_DIR) ||
  ["whisper-base", "silero-vad", "voice-eres2net"].every((m) => fs.existsSync(path.join(devModels, m)));

const MAKE_MEDIA = `
import subprocess, sys
from pathlib import Path
from mcsync.media.tools import find_tools
ffmpeg = find_tools().ffmpeg
dialog, out, offset = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)
subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(dialog), "-ar", "48000", "-ac", "2", str(out / "ZOOM0001.WAV")],
               check=True)
enc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
video = ["-c:v", "libx264", "-pix_fmt", "yuv420p"] if "libx264" in enc else ["-c:v", "mpeg4"]
subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=16", "-ss", str(offset),
                "-i", str(dialog), "-map", "0:v", "-map", "1:a", *video, "-c:a", "aac", "-ar", "48000", "-shortest",
                str(out / "C0001.MOV")], check=True)
`;

let work: string;
let app: ElectronApplication;
let page: Page;

test.describe.configure({ mode: "serial" });
test.skip(!haveModels, "the speech models are not installed (python engine/scripts/fetch_models.py)");

async function shot(name: string): Promise<void> {
  await page.screenshot({ path: path.join(screens, `${name}.png`) });
}

async function answerDialogs(answers: { save?: string; open?: string[] }): Promise<void> {
  await app.evaluate(({ dialog: d }, { save, open }) => {
    d.showSaveDialog = (async () => ({ canceled: !save, filePath: save ?? "" })) as typeof d.showSaveDialog;
    d.showOpenDialog = (async () => ({ canceled: !open, filePaths: open ?? [] })) as typeof d.showOpenDialog;
  }, answers);
}

async function startOf(name: string): Promise<number> {
  return Number(await page.getByTestId(`clip-${name}`).getAttribute("data-start"));
}

test.beforeAll(async () => {
  work = fs.mkdtempSync(path.join(os.tmpdir(), "syncora-analyze-"));
  execFileSync(python, ["-c", MAKE_MEDIA, dialog, path.join(work, "Toast"), String(OFFSET)]);
  fs.mkdirSync(screens, { recursive: true });
  app = await electron.launch({
    ...(packagedApp ? { executablePath: packagedApp } : { args: [appDir] }),
    env: {
      ...process.env,
      MCSYNC_NO_SANDBOX: "1",
      MCSYNC_NO_SPLASH: "1",
      MCSYNC_CACHE_DIR: path.join(work, "cache"),
      MCSYNC_USER_DATA: path.join(work, "user-data"),
    },
  });
  page = await mainWindow(app);
  await page.setViewportSize({ width: 1440, height: 900 });
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

test("imports and synchronises a recorder and a camera", async () => {
  await expect(page.getByTestId("new-project")).toBeEnabled();
  await answerDialogs({ save: path.join(work, "Toast.syncora"), open: [path.join(work, "Toast")] });
  await page.getByTestId("new-project").click();
  await page.getByTestId("import-folder").click();
  await expect(page.getByTestId("bin-clip-ZOOM0001.WAV")).toBeVisible();
  await expect(page.getByTestId("bin-clip-C0001.MOV")).toBeVisible();
  await page.getByTestId("sync").click();
  await expect(page.getByTestId("results")).toBeVisible({ timeout: 180_000 });
});

test("the Analyze stage is open and ready", async () => {
  await page.getByTestId("stage-analyze").click();
  await expect(page.getByTestId("analyze-screen")).toBeVisible();
  await expect(page.getByTestId("analyze-stages")).toContainText("Transcription");
  await expect(page.getByTestId("transcribe-project")).toBeEnabled();
  await shot("20-analyze");
});

test("transcribes the project on this computer", async () => {
  await page.getByTestId("transcribe-project").click();
  const clips = page.getByTestId("transcript-clips");
  await expect(clips).toContainText("ZOOM0001.WAV", { timeout: 60_000 });
  await expect(clips).toContainText("Transcribed", { timeout: 240_000 });
  await expect(page.getByTestId("analyze-stages")).toContainText("detected");
  await shot("21-transcribed");
});

test("shows what was said, by whom", async () => {
  await page.getByTestId("analyze-tab-transcript").click();
  const rows = page.getByTestId("transcript-rows");
  await expect(rows).toContainText(/happy couple/i);
  await expect(rows).toContainText(/raise your glasses/i);
  await expect(page.getByTestId("speakers-column")).toContainText(/Speakers · [12] detected/);

  // Rename the first speaker: every line of theirs follows.
  const column = page.getByTestId("speakers-column");
  await column.locator(".sy-speaker__name").first().click();
  await page.keyboard.type("Father of the bride");
  await page.keyboard.press("Enter");
  await expect(rows).toContainText("FATHER OF THE BRIDE");
  await expect(column).toContainText("Father of the bride");

  // The language popover lists Whisper's languages.
  await page.getByTestId("language-button").click();
  await expect(page.getByRole("listbox", { name: "Transcription language" })).toContainText("Swahili");
  await page.keyboard.press("Escape");
  await shot("22-transcript");
});

test("plays the recording through the media protocol", async () => {
  const src = await page.locator(".sy-transcript__frame video").getAttribute("src");
  expect(src).toMatch(/^syncora-media:\/\//);
  // Byte ranges are served (the player seeks with them), and only project media is served at all.
  const ranged = await page.evaluate(async (url) => {
    const r = await fetch(url!, { headers: { Range: "bytes=0-99" } });
    return { status: r.status, length: (await r.arrayBuffer()).byteLength, range: r.headers.get("content-range") };
  }, src);
  expect(ranged).toMatchObject({ status: 206, length: 100 });
  expect(ranged.range).toMatch(/^bytes 0-99\/\d+$/);
  const denied = await page.evaluate(async () => {
    const file = navigator.platform.startsWith("Win") ? "C:\\Windows\\win.ini" : "/etc/hosts";
    return (await fetch(`syncora-media://media/${encodeURIComponent(file)}`)).status;
  });
  expect(denied).toBe(403);

  await page.getByTestId("transcript-play").click();
  await expect
    .poll(() =>
      page.evaluate(() => document.querySelector<HTMLVideoElement>(".sy-transcript__frame video")?.currentTime ?? 0),
    )
    .toBeGreaterThan(0.3);
  await page.getByTestId("transcript-play").click(); // pause
});

test("finds a sentence with ⌘K and marks it", async () => {
  await page.keyboard.press(process.platform === "darwin" ? "Meta+K" : "Control+K");
  const search = page.getByTestId("moment-search");
  await expect(search).toBeFocused();
  await search.fill("raise your glasses");
  const hit = page.getByTestId("search-hit").first();
  await expect(hit).toContainText(/raise your glasses/i);
  await expect(hit).toContainText("100%");
  await shot("23-search");

  await search.press(process.platform === "darwin" ? "Meta+Enter" : "Control+Enter");
  await page.getByTestId("analyze-tab-markers").click();
  await expect(page.getByTestId("markers-view")).toContainText("raise your glasses");

  // Search by the speaker's new name.
  await page.getByTestId("analyze-tab-search").click();
  await page.getByTestId("moment-search").fill("Father of the bride");
  await expect(page.getByTestId("search-hit").first()).toContainText("FATHER OF THE BRIDE");
});

test("places the camera by AI sync from what was said", async () => {
  await page.getByTestId("stage-timeline").click();
  const box = await page.evaluate(() => window.mcsyncTimeline?.clipRect("C0001.MOV") ?? null);
  expect(box).not.toBeNull();
  await page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await expect(page.getByTestId("inspector")).toContainText("C0001.MOV");
  await page.keyboard.press("Shift+A");

  await expect(page.getByTestId("ai-sync")).toBeVisible();
  const result = page.getByTestId("ai-result");
  await expect(result).toContainText("Match found", { timeout: 180_000 });
  await expect(page.getByTestId("lane-speech")).toContainText("in both");
  const offset = await page.getByTestId("ai-offset").textContent();
  expect(offset).toMatch(/^\+00:0[5]\.[23]/);
  await shot("24-ai-sync");

  await page.getByTestId("ai-accept").click();
  await expect(page.getByTestId("workspace")).toBeVisible();
  await expect.poll(async () => (await startOf("C0001.MOV")) - (await startOf("ZOOM0001.WAV"))).toBeCloseTo(OFFSET, 1);
});

test("the AI and transcription settings are live", async () => {
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-ai").click();
  await expect(page.getByTestId("settings")).toContainText("Ready");
  await expect(page.getByRole("switch", { name: "AI fallback" })).toBeEnabled();
  await page.getByTestId("settings-transcription").click();
  await expect(page.getByRole("combobox", { name: "Speech model" })).toBeVisible();
  for (const pane of ["appearance", "media", "proxy", "export", "shortcuts", "privacy", "updates"]) {
    await page.getByTestId(`settings-${pane}`).click();
    await expect(page.locator(".sy-settings__h1")).toBeVisible();
  }
  await page.getByRole("button", { name: "Close settings" }).click();
});
