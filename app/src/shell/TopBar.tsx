// SH-1 project top bar: symbol, project name, stage tabs 01–05, right cluster (Screen spec, shared parts).
import { Search, Settings } from "lucide-react";
import { useEffect } from "react";

import { Button, SyncoraSymbol, shortcut } from "../design-system/components";
import { type Stage, useProd } from "../state/production";
import { useApp } from "../state/store";

const STAGES: { id: Stage; label: string; disabled?: string }[] = [
  { id: "media", label: "Media" },
  { id: "sync", label: "Sync" },
  {
    id: "analyze",
    label: "Analyze",
    disabled: "Transcription, speaker and AI analysis are not included in this version of Syncora.",
  },
  { id: "timeline", label: "Timeline" },
  { id: "export", label: "Export" },
];

export function TopBar() {
  const project = useApp((s) => s.project);
  const stage = useProd((s) => s.stage);
  const review = useProd((s) => s.summary?.counts.review ?? 0);
  const running = useProd((s) => s.pipeline?.state === "running");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
      const n = Number(e.key);
      const target = STAGES[n - 1];
      if (target && !target.disabled) {
        e.preventDefault();
        go(target.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <header className="sy-topbar" data-testid="toolbar">
      <SyncoraSymbol size={20} />
      <span className="sy-topbar__name">{project?.name}</span>
      <nav className="sy-stage-tabs" aria-label="Stages">
        {STAGES.map((s, k) => (
          <button
            key={s.id}
            type="button"
            className="sy-stage-tab"
            aria-current={stage === s.id ? "page" : undefined}
            aria-disabled={s.disabled ? true : undefined}
            title={s.disabled ?? `${s.label} (${shortcut(`⌘${k + 1}`)})`}
            data-testid={`stage-${s.id}`}
            onClick={() => !s.disabled && go(s.id)}
          >
            <span className="sy-stage-tab__num">{String(k + 1).padStart(2, "0")}</span>
            {s.label}
            {s.id === "sync" && review > 0 && <span className="sy-stage-tab__dot" aria-label={`${review} to review`} />}
          </button>
        ))}
      </nav>
      <div className="sy-topbar__right">
        {stage !== "sync" && (
          <Button
            variant="primary"
            size="compact"
            className="sy-syncall"
            onClick={() => void useProd.getState().startSync()}
            disabled={running && useProd.getState().pipeline?.sync !== null}
            data-testid="sync"
            shortcut="⇧S"
          >
            <SyncoraSymbol size={14} variant="paper" />
            Sync all
          </Button>
        )}
        <button
          type="button"
          className="sy-btn sy-btn--icon"
          aria-label="Search media"
          title={`Search media (${shortcut("⌘F")})`}
          onClick={() => {
            useProd.getState().setStage("media");
            requestAnimationFrame(() =>
              document.querySelector<HTMLInputElement>("[data-testid=media-search]")?.focus(),
            );
          }}
        >
          <Search size={14} aria-hidden />
        </button>
        <button
          type="button"
          className="sy-btn sy-btn--icon"
          aria-label="Settings"
          title={`Settings (${shortcut("⌘,")})`}
          data-testid="open-settings"
          onClick={() => useProd.getState().openSettings()}
        >
          <Settings size={16} aria-hidden />
        </button>
      </div>
    </header>
  );
}

function go(stage: Stage): void {
  const prod = useProd.getState();
  if (stage === "export") {
    useApp.getState().openExport();
    return;
  }
  prod.setStage(stage);
  if (stage === "timeline") useApp.getState().fit();
}
