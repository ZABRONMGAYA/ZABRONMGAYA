// The only surface the renderer has onto the system: `window.mcsync`.
import { contextBridge, ipcRenderer, webUtils } from "electron";

import type { Bridge, EngineEvent } from "../src/api/contract";

const bridge: Bridge = {
  // An {ok, result | error} envelope: custom error fields do not survive the context bridge.
  invoke: (method, params) => ipcRenderer.invoke("engine:invoke", method, params),
  onEvent(listener) {
    const handler = (_e: Electron.IpcRendererEvent, event: EngineEvent) => listener(event);
    ipcRenderer.on("engine:event", handler);
    return () => {
      ipcRenderer.removeListener("engine:event", handler);
    };
  },
  engineStatus: () => ipcRenderer.invoke("engine:status"),
  chooseMedia: (kind) => ipcRenderer.invoke("dialog:media", kind),
  chooseProjectToOpen: () => ipcRenderer.invoke("dialog:open-project"),
  chooseProjectToCreate: (defaultName) => ipcRenderer.invoke("dialog:create-project", defaultName),
  chooseExportPath: (defaultName, format) => ipcRenderer.invoke("dialog:export", defaultName, format),
  showInFolder: (file) => ipcRenderer.invoke("shell:show", file),
  pathForFile: (file) => webUtils.getPathForFile(file),
  readPeaks: (directory, file, offset, length) => ipcRenderer.invoke("peaks:read", directory, file, offset, length),
  platform: process.platform,
};

contextBridge.exposeInMainWorld("mcsync", bridge);
