// Electron main process: window, menus, dialogs, and the engine child process.
// It holds no business logic: renderer calls go to the engine unchanged (allow-listed).
import { createReadStream, existsSync, promises as fs } from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { fileURLToPath } from "node:url";

import { BrowserWindow, Menu, app, dialog, ipcMain, protocol, shell } from "electron";

import {
  ENGINE_METHODS,
  type EngineEvent,
  type InvokeResponse,
  type MenuCommand,
  type Method,
} from "../src/api/contract";
import { EngineProcess, RpcError, engineCommand } from "./engine";
import { PreviewServer, ffmpegPath } from "./preview";

const here = path.dirname(fileURLToPath(import.meta.url));
const allowed = new Set<string>(ENGINE_METHODS);
const PROJECT_FILTER = { name: "Syncora project", extensions: ["syncora"] };
// Projects from Multicam Sync 0.1 open (and are upgraded) too.
const OPEN_FILTER = { name: "Syncora project", extensions: ["syncora", "mcsync"] };
const EXPORT_FILTERS: Record<"xmeml" | "fcpxml", Electron.FileFilter> = {
  xmeml: { name: "FCP 7 XML (Premiere Pro, DaVinci Resolve)", extensions: ["xml"] },
  fcpxml: { name: "FCPXML (DaVinci Resolve, Final Cut Pro)", extensions: ["fcpxml"] },
};
// Files this session exported: the only ones the renderer may reveal in the file manager.
const exported = new Set<string>();
// Media files of the open project (as the engine listed them): the only files the player may stream.
const playable = new Set<string>();
const MEDIA_SCHEME = "syncora-media";

// The player streams project media through its own scheme, with byte ranges so video can seek.
protocol.registerSchemesAsPrivileged([
  {
    scheme: MEDIA_SCHEME,
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true, corsEnabled: true },
  },
]);

if (process.env.MCSYNC_NO_SANDBOX === "1") {
  app.commandLine.appendSwitch("no-sandbox"); // CI containers running as root
}
if (process.env.MCSYNC_USER_DATA) {
  app.setPath("userData", process.env.MCSYNC_USER_DATA); // isolated settings for tests
}

const engine = new EngineProcess(() =>
  engineCommand({ packaged: app.isPackaged, resourcesPath: process.resourcesPath }),
);
let window: BrowserWindow | null = null;
// Pictures and sound for the multicamera preview, of the open project's media only.
let preview: PreviewServer | null = null;
function previewServer(): PreviewServer {
  preview ??= new PreviewServer(ffmpegPath({ packaged: app.isPackaged, resourcesPath: process.resourcesPath }), (f) =>
    playable.has(f),
  );
  return preview;
}

function send(event: EngineEvent): void {
  window?.webContents.send("engine:event", event);
}

engine.on("event", (event: EngineEvent) => send(event));
engine.on("status", (status) => send({ method: "engine.status", params: status }));

/** The Syncora icon for the title bar and taskbar (macOS uses the app bundle's icon). */
function windowIcon(): string | undefined {
  if (process.platform === "darwin") return undefined;
  const file = app.isPackaged
    ? path.join(process.resourcesPath, "icon.png")
    : path.join(here, "..", "build", "icon.png");
  return existsSync(file) ? file : undefined;
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    backgroundColor: "#201e1d", // --sy-bg: no white flash before the renderer paints
    title: "Syncora",
    icon: windowIcon(),
    show: false,
    webPreferences: {
      preload: path.join(here, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  // Shown by `startUp` once the engine and the interface are ready (the splash covers the wait).
  // The renderer is a local app: no navigation away from it, links open in the browser.
  win.webContents.on("will-navigate", (e) => e.preventDefault());
  // A renderer that crashed or was killed (for example out of memory) is reloaded instead of leaving an empty
  // window; the engine keeps the project open and the page picks it up again.
  let reloads: number[] = [];
  win.webContents.on("render-process-gone", (_event, details) => {
    engine.note(`window renderer gone: ${details.reason} (exit code ${details.exitCode})`);
    if (details.reason === "clean-exit" || win.isDestroyed()) return;
    // A page that keeps crashing is left alone after three tries a minute.
    reloads = [...reloads.filter((t) => Date.now() - t < 60_000), Date.now()];
    if (reloads.length <= 3) setTimeout(() => !win.isDestroyed() && win.webContents.reload(), 500);
  });
  win.on("unresponsive", () => engine.note("window not responding"));
  win.on("responsive", () => engine.note("window responding again"));
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) void shell.openExternal(url);
    return { action: "deny" };
  });
  if (process.env.MCSYNC_RENDERER_URL) void win.loadURL(process.env.MCSYNC_RENDERER_URL);
  else void win.loadFile(path.join(here, "..", "dist", "index.html"));
  return win;
}

/** S00 splash: 960 × 560, frameless, shown while the engine starts and the interface loads. */
function createSplash(): BrowserWindow {
  const win = new BrowserWindow({
    width: 960,
    height: 560,
    frame: false,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    center: true,
    show: false,
    backgroundColor: "#201e1d",
    title: "Syncora",
    icon: windowIcon(),
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false, spellcheck: false },
  });
  win.once("ready-to-show", () => win.show());
  win.webContents.on("will-navigate", (e) => e.preventDefault());
  const query = { version: app.getVersion() };
  if (process.env.MCSYNC_RENDERER_URL)
    void win.loadURL(`${process.env.MCSYNC_RENDERER_URL}/splash.html?${new URLSearchParams(query)}`);
  else void win.loadFile(path.join(here, "..", "dist", "splash.html"), { query });
  return win;
}

/** The logo reveal (Motion Specification §1) plays once, in full, and holds briefly before the splash closes. */
const LOGO_REVEAL_MS = 2400;
const LOGO_HOLD_MS = 400;

/**
 * Starts the engine behind the splash, reporting each real step, then swaps the splash for the main window. The
 * main window exists (hidden) from the start, so the interface loads in parallel.
 */
async function startUp(main: BrowserWindow, splash: BrowserWindow | null): Promise<void> {
  const loaded = splash
    ? new Promise<void>((resolve) => {
        splash.webContents.once("did-finish-load", () => resolve());
        splash.once("closed", () => resolve());
      })
    : Promise.resolve();
  const step = async (text: string, percent: number, failed = false) => {
    await loaded;
    if (!splash || splash.isDestroyed()) return;
    await splash.webContents
      .executeJavaScript(`window.splashStep(${JSON.stringify({ text, percent, failed })})`)
      .catch(() => undefined);
  };
  const interfaceReady = new Promise<void>((resolve) => main.once("ready-to-show", () => resolve()));
  const revealStarted = loaded.then(() => Date.now()); // the animation starts with the page
  try {
    void step("Starting audio engine…", 6);
    let hello: Awaited<ReturnType<EngineProcess["start"]>> | null = null;
    try {
      hello = await engine.start();
    } catch {
      // The status (with the engine's error output) is shown in the main window's status bar.
    }
    if (hello) {
      const ffmpeg = hello.ffmpeg?.match(/ffmpeg version (\S+)/)?.[1];
      await step(ffmpeg ? `Loading codecs: FFmpeg ${ffmpeg}…` : "Loading codecs…", 30);
      const machine = hello.plan?.reason;
      await step(machine ? `Checking processors and memory: ${machine}…` : "Checking processors and memory…", 50);
      await step("Loading sync and speech models…", 65);
      await engine.request("ai.status", {}, 60_000).catch(() => undefined); // problems show in the Analyze stage
    } else {
      await step("The audio engine did not start. The status bar shows why.", 65, true);
    }
    await step("Restoring workspace…", 85);
    await Promise.race([interfaceReady, new Promise((r) => setTimeout(r, 30_000))]);
    await step("Ready.", 100);
    const hold = splash ? (await revealStarted) + LOGO_REVEAL_MS + LOGO_HOLD_MS - Date.now() : 0;
    await new Promise((r) => setTimeout(r, Math.max(0, hold)));
  } catch (err) {
    engine.note(`start-up: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`);
  } finally {
    // Whatever happened above, the app is usable: its window shows the engine's state.
    if (!main.isDestroyed()) main.show();
    if (splash && !splash.isDestroyed()) splash.close();
  }
}

function menuCommand(command: MenuCommand) {
  return () => send({ method: "menu", params: { command } });
}

function buildMenu(): void {
  const isMac = process.platform === "darwin";
  const template: Electron.MenuItemConstructorOptions[] = [
    ...(isMac ? [{ role: "appMenu" as const }] : []),
    {
      label: "File",
      submenu: [
        { label: "New Project…", accelerator: "CmdOrCtrl+N", click: menuCommand("new-project") },
        { label: "Open Project…", accelerator: "CmdOrCtrl+O", click: menuCommand("open-project") },
        { type: "separator" },
        { label: "Import Files…", accelerator: "CmdOrCtrl+I", click: menuCommand("import-files") },
        { label: "Import Folder…", accelerator: "CmdOrCtrl+Shift+I", click: menuCommand("import-folder") },
        { type: "separator" },
        { label: "Export XML…", accelerator: "CmdOrCtrl+E", click: menuCommand("export") },
        { type: "separator" },
        { label: "Settings…", accelerator: "CmdOrCtrl+,", click: menuCommand("settings") },
        { type: "separator" },
        { label: "Close Project", accelerator: "CmdOrCtrl+W", click: menuCommand("close-project") },
        ...(isMac ? [] : [{ type: "separator" as const }, { role: "quit" as const }]),
      ],
    },
    {
      label: "Edit",
      submenu: [
        // Text fields keep native undo; elsewhere these undo timeline corrections.
        { label: "Undo", accelerator: "CmdOrCtrl+Z", click: menuCommand("undo") },
        { label: "Redo", accelerator: "CmdOrCtrl+Shift+Z", click: menuCommand("redo") },
        { type: "separator" },
        { label: "Search Moments…", accelerator: "CmdOrCtrl+K", click: menuCommand("search") },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" },
      ],
    },
    {
      label: "Sync",
      submenu: [{ label: "Sync All", accelerator: "Shift+S", click: menuCommand("sync") }],
    },
    {
      label: "View",
      submenu: [
        { label: "Zoom In", accelerator: "CmdOrCtrl+=", click: menuCommand("zoom-in") },
        { label: "Zoom Out", accelerator: "CmdOrCtrl+-", click: menuCommand("zoom-out") },
        { label: "Zoom to Fit", accelerator: "CmdOrCtrl+0", click: menuCommand("zoom-fit") },
        { type: "separator" },
        { role: "toggleDevTools" },
        { role: "togglefullscreen" },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function clampHeight(h: number): number {
  return Math.min(2160, Math.max(144, Math.round(Number(h) || 360)));
}

/** Remember the media files the engine lists for the open project (the player may stream only those). */
function rememberMedia(method: string, result: unknown): void {
  if (method === "project.open" || method === "project.create" || method === "project.close") {
    playable.clear();
    preview?.reset();
  }
  if (method === "media.list") {
    for (const c of (result as { clips: { path: string }[] }).clips) playable.add(path.resolve(c.path));
  } else if (method === "media.index") {
    const index = result as { columns: string[]; rows: unknown[][] };
    const k = index.columns.indexOf("path");
    if (k >= 0) for (const row of index.rows) if (typeof row[k] === "string") playable.add(path.resolve(row[k]));
  }
}

const MEDIA_TYPES: Record<string, string> = {
  ".mp4": "video/mp4",
  ".m4v": "video/mp4",
  ".mov": "video/quicktime",
  ".mkv": "video/x-matroska",
  ".webm": "video/webm",
  ".avi": "video/x-msvideo",
  ".mts": "video/mp2t",
  ".m2ts": "video/mp2t",
  ".ts": "video/mp2t",
  ".wav": "audio/wav",
  ".mp3": "audio/mpeg",
  ".m4a": "audio/mp4",
  ".aac": "audio/aac",
  ".flac": "audio/flac",
  ".ogg": "audio/ogg",
  ".opus": "audio/ogg",
};

/** Stream a project media file, honouring the byte range the player asks for. */
async function serveMedia(request: Request): Promise<Response> {
  const file = path.resolve(decodeURIComponent(new URL(request.url).pathname.slice(1)));
  if (!playable.has(file)) {
    return new Response("not a media file of the open project", {
      status: 403,
      headers: { "Access-Control-Allow-Origin": "*" },
    });
  }
  let size: number;
  try {
    size = (await fs.stat(file)).size;
  } catch {
    return new Response("offline", { status: 404 });
  }
  const type = MEDIA_TYPES[path.extname(file).toLowerCase()] ?? "application/octet-stream";
  // The app's own pages load from file://; nothing else can reach this scheme.
  const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "Content-Range, Content-Length" };
  const body = (start: number, end: number) =>
    Readable.toWeb(createReadStream(file, { start, end })) as unknown as ReadableStream<Uint8Array>;
  const range = /^bytes=(\d*)-(\d*)$/.exec(request.headers.get("range") ?? "");
  if (!range || size === 0) {
    return new Response(size ? body(0, size - 1) : null, {
      status: 200,
      headers: { ...cors, "Content-Type": type, "Content-Length": String(size), "Accept-Ranges": "bytes" },
    });
  }
  let start = range[1] ? Number(range[1]) : Math.max(0, size - Number(range[2] || 0));
  let end = range[1] && range[2] ? Math.min(Number(range[2]), size - 1) : size - 1;
  if (start >= size || start > end) {
    return new Response(null, { status: 416, headers: { ...cors, "Content-Range": `bytes */${size}` } });
  }
  start = Math.max(0, start);
  end = Math.max(start, end);
  return new Response(body(start, end), {
    status: 206,
    headers: {
      ...cors,
      "Content-Type": type,
      "Content-Length": String(end - start + 1),
      "Content-Range": `bytes ${start}-${end}/${size}`,
      "Accept-Ranges": "bytes",
    },
  });
}

function registerIpc(): void {
  ipcMain.handle("engine:invoke", async (_e, method: string, params: unknown): Promise<InvokeResponse<unknown>> => {
    if (!allowed.has(method)) return { ok: false, error: { code: -32601, message: `not allowed: ${method}` } };
    try {
      const result = await engine.request(method as Method, params ?? {});
      if (method === "export.xml") exported.add(path.resolve((result as { path: string }).path));
      rememberMedia(method, result);
      return { ok: true, result };
    } catch (err) {
      const e = err instanceof RpcError ? err : new RpcError(-32603, String(err));
      return { ok: false, error: { code: e.code, message: e.message } };
    }
  });
  ipcMain.handle("engine:status", () => engine.status);
  // The multicamera preview (preview.ts): frames while scrubbing, frame streams while playing, monitor sound.
  ipcMain.handle("preview:caps", async () => ({
    ...(await previewServer().capabilities()),
    videoDecode: String((app.getGPUFeatureStatus() as unknown as Record<string, string>).video_decode ?? "unknown"),
  }));
  ipcMain.handle("preview:frame", async (_e, file: string, t: number, height: number, slot: string) => {
    const jpeg = await previewServer().frame(file, Number(t), clampHeight(height), String(slot));
    return jpeg ? new Uint8Array(jpeg.buffer, jpeg.byteOffset, jpeg.byteLength) : null;
  });
  ipcMain.handle("preview:open", (_e, file: string, start: number, fps: number, height: number) =>
    previewServer().open(file, Number(start), Math.min(60, Math.max(1, Number(fps) || 25)), clampHeight(height)),
  );
  ipcMain.handle("preview:at", (_e, id: string, t: number) => {
    const r = previewServer().at(String(id), Number(t));
    return r === null || r === "reopen" ? r : new Uint8Array(r.buffer, r.byteOffset, r.byteLength);
  });
  ipcMain.handle("preview:close", (_e, id: string) => previewServer().close(String(id)));
  ipcMain.handle(
    "preview:audio",
    async (_e, file: string, start: number, seconds: number, rate: number, stream: number | null) => {
      const pcm = await previewServer().audio(
        file,
        Number(start),
        Math.min(30, Math.max(0.05, Number(seconds))),
        Math.min(96000, Math.max(8000, Number(rate) || 48000)),
        stream === null || stream === undefined ? null : Number(stream),
      );
      return pcm ? new Float32Array(pcm.buffer.slice(pcm.byteOffset, pcm.byteOffset + pcm.byteLength)) : null;
    },
  );
  ipcMain.handle("dialog:media", async (_e, kind: "files" | "folder") => {
    const result = await dialog.showOpenDialog(window!, {
      title: kind === "folder" ? "Import a folder of footage" : "Import media files",
      properties: kind === "folder" ? ["openDirectory", "multiSelections"] : ["openFile", "multiSelections"],
    });
    return result.canceled ? [] : result.filePaths;
  });
  ipcMain.handle("dialog:open-project", async () => {
    const result = await dialog.showOpenDialog(window!, { properties: ["openFile"], filters: [OPEN_FILTER] });
    return result.canceled ? null : (result.filePaths[0] ?? null);
  });
  ipcMain.handle("dialog:folder", async (_e, title: string) => {
    const result = await dialog.showOpenDialog(window!, { title, properties: ["openDirectory"] });
    return result.canceled ? null : (result.filePaths[0] ?? null);
  });
  ipcMain.handle("dialog:file", async (_e, title: string) => {
    const result = await dialog.showOpenDialog(window!, { title, properties: ["openFile"] });
    return result.canceled ? null : (result.filePaths[0] ?? null);
  });
  ipcMain.handle("dialog:save-report", async (_e, defaultName: string, contents: string) => {
    const result = await dialog.showSaveDialog(window!, {
      title: "Save sync report",
      defaultPath: `${defaultName}.json`,
      filters: [{ name: "JSON report", extensions: ["json"] }],
    });
    if (result.canceled || !result.filePath) return null;
    await fs.writeFile(result.filePath, contents, "utf-8");
    return result.filePath;
  });
  ipcMain.handle("dialog:create-project", async (_e, defaultName: string) => {
    const result = await dialog.showSaveDialog(window!, {
      title: "New project",
      defaultPath: `${defaultName}.syncora`,
      filters: [PROJECT_FILTER],
    });
    return result.canceled || !result.filePath ? null : result.filePath;
  });
  ipcMain.handle("dialog:export", async (_e, defaultName: string, format: keyof typeof EXPORT_FILTERS) => {
    const filter = EXPORT_FILTERS[format] ?? EXPORT_FILTERS.xmeml;
    const result = await dialog.showSaveDialog(window!, {
      title: "Export timeline",
      defaultPath: `${defaultName}.${filter.extensions[0]}`,
      filters: [filter],
    });
    return result.canceled || !result.filePath ? null : result.filePath;
  });
  ipcMain.handle("shell:show", (_e, file: string) => {
    const full = path.resolve(file);
    if (exported.has(full)) shell.showItemInFolder(full);
  });
  // Poster frames live in the engine's cache too: only PNG files under its thumbs folder can be read.
  ipcMain.handle("thumb:read", async (_e, file: string) => {
    const cacheDir = engine.status.hello?.cache_dir;
    if (!cacheDir) throw new Error("engine not ready");
    const root = path.resolve(cacheDir, "thumbs") + path.sep;
    const full = path.resolve(file);
    if (!full.startsWith(root) || !full.endsWith(".png")) throw new Error("path outside the cache");
    const data = await fs.readFile(full);
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  });
  // Waveform overviews live in the engine's cache; only files inside it can be read.
  ipcMain.handle("peaks:read", async (_e, directory: string, file: string, offset: number, length: number) => {
    const cacheDir = engine.status.hello?.cache_dir;
    if (!cacheDir) throw new Error("engine not ready");
    const root = path.resolve(cacheDir) + path.sep;
    const full = path.resolve(directory, file);
    if (!full.startsWith(root) || !/^peaks_\d+\.i8$/.test(file)) throw new Error("path outside the cache");
    const handle = await fs.open(full, "r");
    try {
      const buffer = Buffer.alloc(Math.max(0, Math.min(length, 64 * 1024 * 1024)));
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, offset);
      return new Uint8Array(buffer.buffer, buffer.byteOffset, bytesRead);
    } finally {
      await handle.close();
    }
  });
}

app.whenReady().then(async () => {
  protocol.handle(MEDIA_SCHEME, (request) => serveMedia(request));
  registerIpc();
  buildMenu();
  // The main window first (tests and the renderer address it as the app's first window), then the splash.
  window = createWindow();
  window.on("closed", () => {
    window = null;
  });
  const splash = process.env.MCSYNC_NO_SPLASH === "1" ? null : createSplash();
  engine.logTo(path.join(app.getPath("userData"), "logs", "engine.log"));
  await startUp(window, splash);
});

// An uncaught error in this process would otherwise open Electron's modal error box, which stops the event loop
// (engine supervision, quitting) until someone dismisses it. Record it instead.
process.on("uncaughtException", (err) => {
  console.error("Syncora main process error:", err);
  engine.note(`main process error: ${err.stack ?? String(err)}`);
});

let quitting = false;
app.on("before-quit", (event) => {
  if (quitting) return;
  event.preventDefault();
  quitting = true;
  void engine.stop().finally(() => app.quit());
});

app.on("will-quit", () => preview?.dispose());

app.on("window-all-closed", () => {
  app.quit();
});
