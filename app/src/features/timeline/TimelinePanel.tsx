// The timeline: group tabs, zoom, the synchronised tracks, and the clips that could not be placed. Before the
// first synchronisation it says what is missing and offers the next step.
import { Import, LoaderCircle, PanelTopClose, PanelTopOpen, Redo2, Undo2, Waypoints } from "lucide-react";
import { useEffect } from "react";

import type { Session } from "../../api/contract";
import { Button, EmptyState, Select, SyncoraSymbol, shortcut } from "../../design-system/components";
import { formatDuration, formatTime } from "../../lib/format";
import { useProd } from "../../state/production";
import { Transport } from "../multicam/Transport";
import { usePlayback } from "../../state/playback";
import { displayedGroup, refreshIfStale, useApp, usePick } from "../../state/store";
import { Tracks } from "./Tracks";

function isTyping(): boolean {
  const el = document.activeElement;
  return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}

/** Escape deselects. (Nudging and the playhead are the Sync workspace's keys: , . and ← →.) */
function useNudgeKeys(): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping() || e.ctrlKey || e.metaKey) return;
      if (e.key === "Escape") useApp.getState().select(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

/** Chips drawn for unplaced clips; "more…" lists them all in the media browser (search "unsynchronized"). */
const UNPLACED_SHOWN = 60;

function UnplacedStrip() {
  const { timeline, selected, select } = usePick("timeline", "selected", "select");
  const unplaced = timeline ? timeline.unsynced : [];
  // Before the first synchronisation every clip is unplaced: the empty state explains that instead.
  if (!timeline || timeline.groups.length === 0 || unplaced.length === 0) return null;
  const more = unplaced.length - UNPLACED_SHOWN;
  return (
    <div className="unplaced" data-testid="unplaced">
      <span className="muted">Not placed ({unplaced.length.toLocaleString()}):</span>
      {unplaced.slice(0, UNPLACED_SHOWN).map((clip) => (
        <button
          key={clip.clip_id}
          className={`chip ${selected === clip.clip_id ? "selected" : ""} ${clip.flags.includes("excluded") ? "excluded" : ""}`}
          onClick={() => select(clip.clip_id)}
          title={`${clip.device_name} · ${clip.path}`}
          data-testid={`unplaced-${clip.name}`}
        >
          {clip.name}
        </button>
      ))}
      {more > 0 && (
        <button
          className="chip"
          onClick={() => {
            useProd.getState().setBin("all");
            useProd.getState().setQuery("unsynchronized");
            useProd.getState().setStage("media");
          }}
          title="Show every clip that is not placed in the media browser"
        >
          {more.toLocaleString()} more…
        </button>
      )}
    </div>
  );
}

/** The import view of the Media stage. */
function openImport(): void {
  useProd.getState().setImporting(true);
  useProd.getState().setStage("media");
}

const PHASE_WORDS: Record<string, string> = {
  waiting: "Waiting for analysis",
  planning: "Finding candidate pairs",
  matching: "Verifying matches",
  extending: "Extended search",
  solving: "Placing clips",
};

/** What the timeline shows when there is nothing to draw yet, and the next step. */
function TimelineEmpty() {
  const loaded = useProd((s) => s.indexVersion >= 0);
  const clips = useProd((s) => s.rows.length);
  const pipeline = useProd((s) => s.pipeline);
  const timeline = useApp((s) => s.timeline);
  const sync = pipeline?.sync;
  const syncing = sync !== null && sync !== undefined && sync.phase !== "done";
  const analyze = pipeline?.stages.analyze;
  const analysed = analyze ? analyze.done + analyze.failed + analyze.skipped + analyze.cancelled : 0;
  const toAnalyse = analyze ? analysed + analyze.pending + analyze.running : 0;

  let content;
  if (!loaded || (clips > 0 && !timeline)) {
    content = <EmptyState icon={LoaderCircle} title="Loading the timeline" body="Reading the project…" />;
  } else if (clips === 0) {
    content = (
      <EmptyState
        icon={Import}
        title="No media yet"
        body="Import the recordings of the event (every camera, recorder and phone), then synchronise them. Original files are never modified."
        action={
          <Button variant="secondary" size="compact" onClick={openImport}>
            Import media
          </Button>
        }
      />
    );
  } else if (syncing) {
    content = (
      <EmptyState
        icon={Waypoints}
        title="Synchronising"
        body={`${PHASE_WORDS[sync.phase] ?? "Working"} · ${clips.toLocaleString()} clips. The timeline appears here when the synchronisation finishes.`}
        action={
          <Button variant="secondary" size="compact" onClick={() => useProd.getState().setStage("sync")}>
            Show progress
          </Button>
        }
      />
    );
  } else if (clips < 2) {
    content = (
      <EmptyState
        icon={Import}
        title="One recording so far"
        body="Import at least two recordings of the same event to line them up."
        action={
          <Button variant="secondary" size="compact" onClick={openImport}>
            Import media
          </Button>
        }
      />
    );
  } else {
    content = (
      <EmptyState
        icon={Waypoints}
        title="Not synchronised yet"
        body={
          <>
            {clips.toLocaleString()} clips are ready to line up on a timeline.
            {toAnalyse > analysed && (
              <>
                {" "}
                Audio analysis: {analysed.toLocaleString()} / {toAnalyse.toLocaleString()} clips
                {pipeline?.state === "paused" ? " (paused)" : ""}.
              </>
            )}
          </>
        }
        action={
          <Button
            variant="primary"
            size="compact"
            onClick={() => void useProd.getState().startSync()}
            data-testid="timeline-sync"
          >
            <SyncoraSymbol size={14} variant="paper" />
            Sync all
          </Button>
        }
      />
    );
  }
  return (
    <div className="timeline-empty" data-testid="timeline-empty">
      {content}
    </div>
  );
}

/** Show or hide the multicamera viewer above the timeline. */
function ViewerToggle() {
  const viewer = usePlayback((s) => s.viewer);
  return (
    <button
      className="small"
      onClick={() => usePlayback.getState().setViewer(!viewer)}
      title={viewer ? "Hide the cameras: the timeline takes the whole height" : "Show the cameras above the timeline"}
      aria-pressed={viewer}
      data-testid="toggle-viewer"
    >
      {viewer ? <PanelTopClose size={14} aria-hidden /> : <PanelTopOpen size={14} aria-hidden />} Cameras
    </button>
  );
}

/** A sync group's name: its session's label when there is one. */
function groupLabel(group: number, sessions: Session[]): string {
  const session = sessions.find((s) => s.group_no === group && s.source === "auto");
  if (session) return session.label + (group === 0 ? " (reference)" : "");
  return group === 0 ? "Reference group" : `Group ${group + 1}`;
}

export function TimelinePanel() {
  const { timeline, group, setGroup, view, timelineWidth, cursorS, fit, updating } = usePick(
    "timeline",
    "group",
    "setGroup",
    "view",
    "timelineWidth",
    "cursorS",
    "fit",
    "updating",
  );
  useNudgeKeys();
  const current = displayedGroup({ timeline, group });
  const sessions = useProd((s) => s.sessions);
  // Load the timeline on arrival, and again while media is still being added or read.
  const indexVersion = useProd((s) => s.indexVersion);
  useEffect(() => refreshIfStale(), [indexVersion]);

  return (
    <section className="timeline" data-testid="timeline">
      <div className="timeline-bar">
        {current && <Transport />}
        {current && <ViewerToggle />}
        {(timeline?.groups.length ?? 0) <= 6 ? (
          <div className="tabs" role="tablist">
            {timeline?.groups.map((g) => (
              <button
                key={g.group}
                role="tab"
                aria-selected={g.group === current?.group}
                className={g.group === current?.group ? "tab active" : "tab"}
                onClick={() => setGroup(g.group)}
                title={
                  g.group === 0
                    ? "The reference recording and everything synced to it"
                    : "A separate sync group (another session): its clips are synced to each other"
                }
                data-testid={`group-${g.group}`}
              >
                {groupLabel(g.group, sessions)} · {g.clips.length}
              </button>
            ))}
          </div>
        ) : (
          <Select
            label="Sync group"
            value={current?.group ?? 0}
            onChange={(g) => setGroup(g)}
            options={(timeline?.groups ?? []).map((g) => ({
              value: g.group,
              label: `${groupLabel(g.group, sessions)} · ${g.clips.length} clips`,
            }))}
          />
        )}
        <div className="grow" />
        {updating && (
          <span className="muted timeline-updating" data-testid="timeline-updating">
            <LoaderCircle size={14} className="sy-spin" aria-hidden /> Updating…
          </span>
        )}
        <button
          className="small"
          onClick={() => void useApp.getState().undo()}
          title={`Undo the last correction (${shortcut("⌘Z")})`}
          aria-label="Undo"
        >
          <Undo2 size={14} aria-hidden /> Undo
        </button>
        <button
          className="small"
          onClick={() => void useApp.getState().redo()}
          title={`Redo (${shortcut("⇧⌘Z")})`}
          aria-label="Redo"
        >
          <Redo2 size={14} aria-hidden /> Redo
        </button>
        {cursorS !== null && (
          <span className="muted" data-testid="cursor-time">
            Playhead {formatTime(cursorS)}
          </span>
        )}
        {current && (
          <>
            <span className="muted">{formatDuration(timelineWidth / view.pxPerSec)} visible</span>
            <button className="small" onClick={fit} title="Show the whole timeline (0)">
              Fit
            </button>
          </>
        )}
      </div>

      {current ? <Tracks group={current} /> : <TimelineEmpty />}
      <UnplacedStrip />
    </section>
  );
}
