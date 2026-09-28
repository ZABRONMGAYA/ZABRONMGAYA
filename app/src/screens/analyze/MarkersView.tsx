// Markers: moments heard while transcribing (applause, music, laughter…) and the ones added by hand (M).
import { Flag, Play, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import type { Marker } from "../../api/contract";
import { Button, EmptyState } from "../../design-system/components";
import { useAnalyze } from "../../state/analyze";
import { useProd } from "../../state/production";
import { clipClock } from "./TranscriptView";

function LabelEditor({ marker, onDone }: { marker: Marker; onDone: () => void }) {
  const [value, setValue] = useState(marker.label ?? "");
  const save = () => {
    const label = value.trim() || null;
    if (label !== (marker.label ?? null)) void useAnalyze.getState().renameMarker(marker.id, label);
    onDone();
  };
  return (
    <input
      className="sy-speaker__input"
      value={value}
      autoFocus
      aria-label="Marker name"
      onChange={(e) => setValue(e.target.value)}
      onBlur={save}
      onKeyDown={(e) => {
        if (e.key === "Enter") save();
        if (e.key === "Escape") onDone();
      }}
    />
  );
}

export function MarkersView() {
  const markers = useAnalyze((s) => s.markers);
  const rows = useProd((s) => s.rows);
  const byId = useMemo(() => new Map(rows.map((r) => [r.clip_id, r])), [rows]);
  const [editing, setEditing] = useState<number | null>(null);
  const [filter, setFilter] = useState<"all" | "ai" | "user">("all");
  const shown = markers.filter((m) => filter === "all" || (filter === "user") === (m.source === "user"));

  if (!markers.length) {
    return (
      <div className="sy-analyze__pane">
        <EmptyState
          icon={Flag}
          title="No markers"
          body="Run AI analysis to find speeches, applause and key moments — or press M."
          action={
            <Button
              variant="secondary"
              size="compact"
              onClick={() => {
                useAnalyze.getState().setTab("overview");
                void useAnalyze.getState().transcribe("smart");
              }}
            >
              Find moments
            </Button>
          }
        />
      </div>
    );
  }
  return (
    <div className="sy-analyze__pane" data-testid="markers-view">
      <div className="sy-analyze__pane-head">
        <h1 className="sy-analyze__h1">Markers · {markers.length}</h1>
        <p className="sy-dim">Red = found by AI · Paper = added by you (M)</p>
        <div className="sy-segmented" role="group" aria-label="Show">
          {(["all", "ai", "user"] as const).map((f) => (
            <button key={f} type="button" aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {f === "all" ? "All" : f === "ai" ? "Found by AI" : "Added by you"}
            </button>
          ))}
        </div>
      </div>
      <div className="sy-marker-list">
        {shown.map((m) => {
          const row = byId.get(m.clip_id);
          return (
            <div key={m.id} className="sy-marker-row">
              <span
                className={`sy-marker-tile sy-marker-tile--static ${m.source === "user" ? "sy-marker-tile--user" : ""}`}
              />
              <span className="sy-marker-row__name">
                {editing === m.id ? (
                  <LabelEditor marker={m} onDone={() => setEditing(null)} />
                ) : (
                  <button type="button" className="sy-speaker__name" onClick={() => setEditing(m.id)} title="Rename">
                    {m.label ?? "Marker"}
                  </button>
                )}
              </span>
              <span className="sy-marker-row__where sy-ellipsis">
                {row?.name ?? `Clip ${m.clip_id}`}
                {row?.device_name ? ` · ${row.device_name}` : ""}
              </span>
              <span className="sy-marker-row__time tnum">{clipClock(row, m.t_s)}</span>
              <span className="sy-marker-row__actions">
                <Button
                  variant="ghost"
                  size="compact"
                  icon={Play}
                  onClick={() => void useAnalyze.getState().openClip(m.clip_id, Math.max(0, m.t_s - 1))}
                >
                  Open
                </Button>
                <button
                  type="button"
                  className="sy-btn sy-btn--icon sy-btn--compact"
                  aria-label={`Delete ${m.label ?? "marker"}`}
                  onClick={() => void useAnalyze.getState().deleteMarker(m.id)}
                >
                  <Trash2 size={12} aria-hidden />
                </button>
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
