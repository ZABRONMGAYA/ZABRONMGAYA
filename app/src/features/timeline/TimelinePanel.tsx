// The timeline: group tabs, zoom, the synchronised tracks, and the clips that could not be placed.
import { useEffect } from "react";

import { formatDuration, formatTime, parseRate } from "../../lib/format";
import { displayedGroup, findClip, useApp, usePick } from "../../state/store";
import { Tracks } from "./Tracks";
import { frameDuration } from "./geometry";

function isTyping(): boolean {
  const el = document.activeElement;
  return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}

/** Arrow keys nudge the selected clip: a frame, ten frames with Shift, a millisecond with Alt. */
function useNudgeKeys(): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping() || e.ctrlKey || e.metaKey) return;
      const { selected, timeline, nudge, select } = useApp.getState();
      if (e.key === "Escape") {
        select(null);
        return;
      }
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const clip = findClip(timeline, selected);
      if (!clip || clip.start_s === null) return;
      e.preventDefault();
      const step = e.altKey ? 0.001 : frameDuration(parseRate(clip.frame_rate)) * (e.shiftKey ? 10 : 1);
      void nudge(clip.clip_id, e.key === "ArrowLeft" ? -step : step);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

function UnplacedStrip() {
  const { timeline, media, selected, select } = usePick("timeline", "media", "selected", "select");
  const unplaced = timeline ? timeline.unsynced : [];
  if (!timeline || unplaced.length === 0) return null;
  const names = new Map(media.clips.map((c) => [c.clip_id, c]));
  return (
    <div className="unplaced" data-testid="unplaced">
      <span className="muted">Not placed ({unplaced.length}):</span>
      {unplaced.map((clip) => (
        <button
          key={clip.clip_id}
          className={`chip ${selected === clip.clip_id ? "selected" : ""} ${clip.flags.includes("excluded") ? "excluded" : ""}`}
          onClick={() => select(clip.clip_id)}
          title={names.get(clip.clip_id)?.path ?? clip.path}
          data-testid={`unplaced-${clip.name}`}
        >
          {clip.name}
        </button>
      ))}
    </div>
  );
}

export function TimelinePanel() {
  const { timeline, group, setGroup, view, timelineWidth, cursorS, fit, media } = usePick(
    "timeline",
    "group",
    "setGroup",
    "view",
    "timelineWidth",
    "cursorS",
    "fit",
    "media",
  );
  useNudgeKeys();
  const current = displayedGroup({ timeline, group });

  return (
    <section className="timeline" data-testid="timeline">
      <div className="timeline-bar">
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
                  : "Clips synced to each other but not to the reference: place one of them by hand to join the main timeline"
              }
              data-testid={`group-${g.group}`}
            >
              {g.group === 0 ? "Main" : `Unlinked ${g.group}`} · {g.clips.length}
            </button>
          ))}
        </div>
        <div className="grow" />
        {cursorS !== null && (
          <span className="muted" data-testid="cursor-time">
            Cursor {formatTime(cursorS)}
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

      {current ? (
        <Tracks group={current} />
      ) : (
        <div className="timeline-empty">
          <p className="muted">
            {media.clips.length < 2
              ? "Import at least two recordings of the same event."
              : "Press Synchronise to line the recordings up on a timeline."}
          </p>
        </div>
      )}
      <UnplacedStrip />
    </section>
  );
}
