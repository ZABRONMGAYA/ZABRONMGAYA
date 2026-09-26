import type { SyncMode } from "../../api/contract";
import { usePick } from "../../state/store";

const MODES: { value: SyncMode; label: string; hint: string }[] = [
  { value: "hybrid", label: "Audio + timecode", hint: "Audio waveforms, guided and completed by timecode and clocks" },
  { value: "audio", label: "Audio only", hint: "Ignore timecode and camera clocks" },
  { value: "timecode", label: "Timecode only", hint: "Frame-accurate jam-synced timecode, no audio analysis" },
];

export function Toolbar() {
  const {
    project,
    media,
    jobs,
    lastSync,
    importMedia,
    sync,
    cancelJob,
    updateSettings,
    undo,
    redo,
    setReference,
    timeline,
    openExport,
  } = usePick(
    "project",
    "media",
    "jobs",
    "lastSync",
    "importMedia",
    "sync",
    "cancelJob",
    "updateSettings",
    "undo",
    "redo",
    "setReference",
    "timeline",
    "openExport",
  );
  if (!project) return null;
  const settings = project.settings;
  const running = Object.values(jobs).filter((j) => j.status === "running");
  const busy = running.length > 0;
  const withAudio = media.clips.filter((c) => c.audio_stream !== null);

  return (
    <header className="toolbar" data-testid="toolbar">
      <div className="group">
        <span className="project-name" title={project.path}>
          {project.name}
        </span>
        <button onClick={() => void importMedia("folder")} disabled={busy} data-testid="import-folder">
          Import folder…
        </button>
        <button onClick={() => void importMedia("files")} disabled={busy}>
          Import files…
        </button>
      </div>

      <div className="group">
        <label title={MODES.find((m) => m.value === settings.mode)?.hint}>
          Sync by
          <select
            value={settings.mode}
            onChange={(e) => void updateSettings({ mode: e.target.value as SyncMode })}
            disabled={busy}
            data-testid="sync-mode"
          >
            {MODES.map((m) => (
              <option key={m.value} value={m.value} title={m.hint}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
        <label className="check" title="All devices were jam-synced to one timecode source">
          <input
            type="checkbox"
            checked={settings.timecode_jam_synced}
            onChange={(e) => void updateSettings({ timecode_jam_synced: e.target.checked })}
            disabled={busy}
            data-testid="jam-synced"
          />
          Jam-synced timecode
        </label>
        <label title="The recording every other clip is placed against (usually the external recorder)">
          Reference
          <select
            value={settings.reference_clip_id ?? ""}
            onChange={(e) => void setReference(e.target.value ? Number(e.target.value) : null)}
            disabled={busy}
            data-testid="reference"
          >
            <option value="">Longest recording</option>
            {withAudio.map((c) => (
              <option key={c.clip_id} value={c.clip_id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <button
          className="primary"
          onClick={() => void sync()}
          disabled={busy || media.clips.length < 2}
          data-testid="sync"
        >
          Synchronise
        </button>
      </div>

      <div className="group grow">
        {running.map((job) => (
          <div key={job.id} className="job" data-testid={`job-${job.kind}`}>
            <div className="progress">
              <div className="bar" style={{ width: `${Math.round(job.progress * 100)}%` }} />
            </div>
            <span className="job-message">{job.message}</span>
            <button className="small" onClick={() => void cancelJob(job.id)}>
              Cancel
            </button>
          </div>
        ))}
        {!busy && lastSync && (
          <span className="muted" data-testid="last-sync">
            {lastSync.pairs} pairs · {lastSync.matched} matched · {lastSync.reused} reused
          </span>
        )}
      </div>

      <div className="group">
        <button onClick={() => void undo()} disabled={busy} title="Undo the last correction">
          Undo
        </button>
        <button onClick={() => void redo()} disabled={busy} title="Redo">
          Redo
        </button>
        <button
          onClick={openExport}
          disabled={busy || !timeline?.groups.length}
          title="Write the timeline for Premiere Pro or DaVinci Resolve (⌘/Ctrl+E)"
          data-testid="export"
        >
          Export XML…
        </button>
      </div>
    </header>
  );
}
