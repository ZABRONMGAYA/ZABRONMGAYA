// Electron main process: window, menus, dialogs, and the engine child process.
// It holds no business logic: renderer calls go to the engine unchanged (allow-listed).
import { existsSync, promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { BrowserWindow, Menu, app, dialog, ipcMain, shell } from "electron";

import {
  ENGINE_METHODS,
  type EngineEvent,
  type InvokeResponse,
  type MenuCommand,
  type Method,
} from "../src/api/contract";
import { EngineProcess, RpcError, engineCommand } from "./engine";

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
  win.once("ready-to-show", () => win.show());
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

function registerIpc(): void {
  ipcMain.handle("engine:invoke", async (_e, method: string, params: unknown): Promise<InvokeResponse<unknown>> => {
    if (!allowed.has(method)) return { ok: false, error: { code: -32601, message: `not allowed: ${method}` } };
    try {
      const result = await engine.request(method as Method, params ?? {});
      if (method === "export.xml") exported.add(path.resolve((result as { path: string }).path));
      return { ok: true, result };
    } catch (err) {
      const e = err instanceof RpcError ? err : new RpcError(-32603, String(err));
      return { ok: false, error: { code: e.code, message: e.message } };
    }
  });
  ipcMain.handle("engine:status", () => engine.status);
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
  registerIpc();
  buildMenu();
  window = createWindow();
  window.on("closed", () => {
    window = null;
  });
  engine.logTo(path.join(app.getPath("userData"), "logs", "engine.log"));
  try {
    await engine.start();
  } catch {
    // Status (with the engine's error output) has been sent to the renderer.
  }
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

app.on("window-all-closed", () => {
  app.quit();
});
