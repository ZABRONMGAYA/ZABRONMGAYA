import { type DragEvent, useEffect, useRef, useState } from "react";

import { bridge, call } from "./api/client";
import { Toasts } from "./components/Toasts";
import { ExportDialog } from "./features/export/ExportDialog";
import { Inspector } from "./features/inspector/Inspector";
import { ReviewQueue } from "./features/inspector/ReviewQueue";
import { TimelinePanel } from "./features/timeline/TimelinePanel";
import { Dialogs } from "./screens/dialogs";
import { Home } from "./screens/home/Home";
import { MediaScreen } from "./screens/media/MediaScreen";
import { Settings, savedWorkers } from "./screens/settings/Settings";
import { SyncScreen } from "./screens/sync/SyncScreen";
import { StatusBar } from "./shell/StatusBar";
import { TopBar } from "./shell/TopBar";
import { useProd } from "./state/production";
import { useApp } from "./state/store";

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
          <Home />
        ) : stage === "media" ? (
          <MediaScreen />
        ) : stage === "sync" ? (
          <SyncScreen />
        ) : (
          <div className="workspace" data-testid="workspace">
            <div className="panes">
              <TimelinePanel />
              <aside className="side">
                <ReviewQueue />
                <Inspector />
              </aside>
            </div>
          </div>
        )}
      </div>
      <StatusBar />
      <ExportDialog />
      <Settings />
      <Dialogs />
      <Toasts />
    </div>
  );
}

function isTyping(): boolean {
  const el = document.activeElement;
  return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}
