import { type DragEvent, useEffect, useRef, useState } from "react";

import { bridge } from "./api/client";
import { StatusBar } from "./components/StatusBar";
import { Toasts } from "./components/Toasts";
import { ExportDialog } from "./features/export/ExportDialog";
import { Inspector } from "./features/inspector/Inspector";
import { ReviewQueue } from "./features/inspector/ReviewQueue";
import { MediaBin } from "./features/media/MediaBin";
import { Toolbar } from "./features/sync/Toolbar";
import { TimelinePanel } from "./features/timeline/TimelinePanel";
import { Welcome } from "./features/welcome/Welcome";
import { useApp } from "./state/store";

export function App() {
  const init = useApp((s) => s.init);
  const project = useApp((s) => s.project);
  const [dropping, setDropping] = useState(false);
  const dragDepth = useRef(0); // enter/leave fire for every child element
  useEffect(() => init(), [init]);

  // Drop folders or files anywhere on the workspace to import them.
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
    <div className="app">
      {project ? (
        <div
          className={`workspace ${dropping ? "dropping" : ""}`}
          onDragEnter={onDragEnter}
          onDragLeave={onDragLeave}
          onDragOver={onDragOver}
          onDrop={onDrop}
        >
          <Toolbar />
          <div className="panes">
            <MediaBin />
            <TimelinePanel />
            <aside className="side">
              <ReviewQueue />
              <Inspector />
            </aside>
          </div>
        </div>
      ) : (
        <Welcome />
      )}
      <StatusBar />
      <ExportDialog />
      <Toasts />
    </div>
  );
}
