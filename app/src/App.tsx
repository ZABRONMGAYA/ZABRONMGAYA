import { type DragEvent, useEffect, useRef, useState } from "react";

import { bridge, call } from "./api/client";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Toasts } from "./components/Toasts";
import { ExportDialog } from "./features/export/ExportDialog";
import { SyncWorkspace } from "./features/multicam/SyncWorkspace";
import { AnalyzeScreen } from "./screens/analyze/AnalyzeScreen";
import { Dialogs } from "./screens/dialogs";
import { Home } from "./screens/home/Home";
import { MediaScreen } from "./screens/media/MediaScreen";
import { Settings, savedWorkers } from "./screens/settings/Settings";
import { SyncScreen } from "./screens/sync/SyncScreen";
import { StatusBar } from "./shell/StatusBar";
import { TopBar } from "./shell/TopBar";
import { useAnalyze } from "./state/analyze";
import { useProd } from "./state/production";
import { openSearch, useApp } from "./state/store";

export function App() {
  const init = useApp((s) => s.init);
  const project = useApp((s) => s.project);
  const engineState = useApp((s) => s.engine.state);
  const stage = useProd((s) => s.stage);
  const [dropping, setDropping] = useState(false);
  const dragDepth = useRef(0); // enter/leave fire for every child element
  useEffect(() => init(), [init]);

  // Worker counts chosen in Settings → Performance apply to every engine start.
  useEffect(() => {
    if (engineState !== "ready") return;
    const workers = savedWorkers();
    if (workers !== "auto") void call("engine.configure", { workers }).catch(() => undefined);
  }, [engineState]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!useApp.getState().project || isTyping()) return;
      const prod = useProd.getState();
      const mod = e.metaKey || e.ctrlKey;
      if (e.key === "S" && e.shiftKey && !mod) {
        e.preventDefault();
        void prod.startSync();
      } else if (mod && e.key === ".") {
        e.preventDefault();
        void (prod.pipeline?.state === "paused" ? prod.resumePipeline() : prod.pause());
      } else if (mod && e.key.toLowerCase() === "b") {
        e.preventDefault();
        prod.setStage("media");
      } else if (mod && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openSearch();
      } else if (e.key === "A" && e.shiftKey && !mod) {
        // ⇧A: AI sync for the selected clip (S07).
        const selected = useApp.getState().selected;
        if (selected !== null) {
          e.preventDefault();
          void useAnalyze.getState().openAiSync(selected);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Drop folders or files anywhere on a project to import them.
  const carriesFiles = (e: DragEvent) => e.dataTransfer.types.includes("Files");
  function onDragEnter(e: DragEvent) {
    if (!carriesFiles(e)) return;
    dragDepth.current += 1;
    setDropping(true);
  }
  function onDragLeave(e: DragEvent) {
    if (!carriesFiles(e)) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDropping(false);
  }
  function onDragOver(e: DragEvent) {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }
  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragDepth.current = 0;
    setDropping(false);
    const paths = Array.from(e.dataTransfer.files, (f) => bridge().pathForFile(f)).filter(Boolean);
    if (paths.length) void useApp.getState().importPaths(paths);
  }

  return (
    <div className={`sy-app ${project ? "" : "sy-app--home"}`}>
      {project && <TopBar />}
      <div
        className={`sy-main ${dropping ? "sy-dropping" : ""}`}
        onDragEnter={project ? onDragEnter : undefined}
        onDragLeave={project ? onDragLeave : undefined}
        onDragOver={project ? onDragOver : undefined}
        onDrop={project ? onDrop : undefined}
      >
        {!project ? (
          <ErrorBoundary key="home" what="the home screen">
            <Home />
          </ErrorBoundary>
        ) : stage === "media" ? (
          <ErrorBoundary key="media" what="the media browser">
            <MediaScreen />
          </ErrorBoundary>
        ) : stage === "sync" ? (
          <ErrorBoundary key="sync" what="the synchronisation screen">
            <SyncScreen />
          </ErrorBoundary>
        ) : stage === "analyze" ? (
          <ErrorBoundary key="analyze" what="the analysis screen">
            <AnalyzeScreen />
          </ErrorBoundary>
        ) : (
          <ErrorBoundary key="timeline" what="the sync workspace">
            <SyncWorkspace />
          </ErrorBoundary>
        )}
      </div>
      <StatusBar />
      <ErrorBoundary what="the export dialog" onClose={() => useApp.getState().closeExport()}>
        <ExportDialog />
      </ErrorBoundary>
      <ErrorBoundary what="the settings" onClose={() => useProd.getState().closeSettings()}>
        <Settings />
      </ErrorBoundary>
      <ErrorBoundary what="the dialog" onClose={() => useProd.getState().openDialog(null)}>
        <Dialogs />
      </ErrorBoundary>
      <Toasts />
    </div>
  );
}

function isTyping(): boolean {
  const el = document.activeElement;
  return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}
