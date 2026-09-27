// Export the synchronised timeline as XML for an NLE, then show what was written and how exactly.
import { type FormEvent, useEffect, useState } from "react";

import { bridge } from "../../api/client";
import type { ExportFormat, ExportReport, Timeline } from "../../api/contract";
import { formatDuration, parseRate, rateLabel } from "../../lib/format";
import { usePick } from "../../state/store";

const SETTINGS_KEY = "mcsync.exportOptions";
const RATES = ["24000/1001", "24", "25", "30000/1001", "30", "50", "60000/1001", "60"];
const TIMECODE = /^\d{2}:[0-5]\d:[0-5]\d[:;]\d{2}$/;

// DESIGN-OPEN: #13 compatibility copy states only what the exporters' golden tests check (DESIGN_DECISIONS.md D-09).
const FORMATS: { value: ExportFormat; title: string; detail: string }[] = [
  {
    value: "xmeml",
    title: "Premiere Pro or DaVinci Resolve",
    detail: "FCP 7 XML (.xml). Clips start on whole frames; Premiere Pro places recorder audio to the sample.",
  },
  {
    value: "fcpxml",
    title: "DaVinci Resolve or Final Cut Pro",
    detail: "FCPXML 1.10 (.fcpxml). Recorder audio is placed to the sample.",
  },
];

interface Settings {
  format: ExportFormat;
  rate: string; // "" = automatic
  startTimecode: string;
  includeUncertain: boolean;
}

const DEFAULTS: Settings = { format: "xmeml", rate: "", startTimecode: "01:00:00:00", includeUncertain: true };

function loadSettings(): Settings {
  try {
    return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(SETTINGS_KEY) ?? "{}") as Partial<Settings>) };
  } catch {
    return DEFAULTS;
  }
}

function saveSettings(settings: Settings): void {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // a convenience only
  }
}

/** The engine's automatic choice: the video rate covering the most footage. */
export function dominantRate(timeline: Timeline, group: number): string | null {
  const totals = new Map<string, number>();
  for (const clip of timeline.groups.find((g) => g.group === group)?.clips ?? []) {
    if (clip.has_video && clip.frame_rate)
      totals.set(clip.frame_rate, (totals.get(clip.frame_rate) ?? 0) + clip.duration_s);
  }
  let best: string | null = null;
  for (const [rate, total] of totals) if (best === null || total > totals.get(best)!) best = rate;
  return best;
}

/** How exactly the NLE will place the clips: cameras on whole frames, recorder audio to the sample where it can. */
export function accuracy(report: ExportReport): string {
  const worst = (track: string) =>
    Math.max(0, ...report.clips.filter((c) => c.track.startsWith(track)).map((c) => Math.abs(c.error_ms)));
  const hasVideo = report.clips.some((c) => c.track.startsWith("V"));
  const hasAudio = report.clips.some((c) => c.track.startsWith("A"));
  const parts: string[] = [];
  if (hasVideo) {
    parts.push(`Cameras start on whole frames, within ${worst("V").toFixed(1)} ms of their synchronised positions.`);
  }
  if (hasAudio) {
    const audio = worst("A");
    const exact = (ms: number) => ms < 0.05;
    if (report.format === "xmeml") {
      parts.push(
        exact(audio)
          ? "Recorder audio is placed to the sample."
          : `Recorder audio is placed to the sample in Premiere Pro, and within ${audio.toFixed(1)} ms in DaVinci Resolve.`,
      );
    } else {
      parts.push(
        exact(audio) ? "Recorder audio is placed to the sample." : `Recorder audio is within ${audio.toFixed(1)} ms.`,
      );
    }
  }
  return parts.join(" ");
}

function Result({ report, onDone }: { report: ExportReport; onDone: () => void }) {
  const seq = report.sequence;
  const name = report.path.split(/[\\/]/).pop();
  return (
    <div className="export-result" data-testid="export-result">
      <p className="lead">
        Exported {report.clips.length} clips to <strong title={report.path}>{name}</strong>.
      </p>
      <p className="muted">
        {rateLabel(seq.rate)} fps · {seq.width}×{seq.height} · starts at {seq.start_timecode} ·{" "}
        {formatDuration(seq.duration_s)} · {seq.video_tracks} video and {seq.audio_tracks} audio tracks
      </p>
      <p data-testid="export-accuracy">{accuracy(report)}</p>
      {report.skipped.length > 0 && (
        <>
          <h3>Not exported</h3>
          <ul className="plain">
            {report.skipped.map((s) => (
              <li key={s.clip_id}>
                {s.name}: <span className="muted">{s.reason}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {report.warnings.length > 0 && (
        <>
          <h3>Check in the NLE</h3>
          <ul className="plain warnings">
            {report.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </>
      )}
      <div className="modal-actions">
        <button onClick={() => void bridge().showInFolder(report.path)}>Show in folder</button>
        <button className="primary" onClick={onDone} autoFocus>
          Done
        </button>
      </div>
    </div>
  );
}

export function ExportDialog() {
  const { exportOpen, closeExport, exportXml, lastExport, timeline, group } = usePick(
    "exportOpen",
    "closeExport",
    "exportXml",
    "lastExport",
    "timeline",
    "group",
  );
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const [exportGroup, setExportGroup] = useState(0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!exportOpen) return;
    setExportGroup(timeline?.groups.some((g) => g.group === group) ? group : 0);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeExport();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [exportOpen, closeExport, group, timeline]);

  if (!exportOpen || !timeline) return null;
  const auto = dominantRate(timeline, exportGroup);
  const reviewCount = timeline.groups
    .find((g) => g.group === exportGroup)
    ?.clips.filter((c) => c.status === "needs_review").length;
  const timecodeOk = TIMECODE.test(settings.startTimecode);
  const update = (changes: Partial<Settings>) => setSettings((s) => ({ ...s, ...changes }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!timecodeOk) return;
    saveSettings(settings);
    setBusy(true);
    try {
      await exportXml({
        format: settings.format,
        group: exportGroup,
        start_timecode: settings.startTimecode,
        include_uncertain: settings.includeUncertain,
        ...(settings.rate ? { sequence_rate: settings.rate } : {}),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={closeExport}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-title"
        onMouseDown={(e) => e.stopPropagation()}
        data-testid="export-dialog"
      >
        <h2 id="export-title">Export timeline</h2>
        {lastExport ? (
          <Result report={lastExport} onDone={closeExport} />
        ) : (
          <form onSubmit={(e) => void submit(e)}>
            <fieldset className="formats">
              <legend>For</legend>
              {FORMATS.map((f) => (
                <label key={f.value} className={`format ${settings.format === f.value ? "chosen" : ""}`}>
                  <input
                    type="radio"
                    name="format"
                    value={f.value}
                    checked={settings.format === f.value}
                    onChange={() => update({ format: f.value })}
                    data-testid={`format-${f.value}`}
                  />
                  <span>
                    <strong>{f.title}</strong>
                    <span className="muted">{f.detail}</span>
                  </span>
                </label>
              ))}
            </fieldset>

            <div className="fields">
              {timeline.groups.length > 1 && (
                <label>
                  Timeline
                  <select value={exportGroup} onChange={(e) => setExportGroup(Number(e.target.value))}>
                    {timeline.groups.map((g) => (
                      <option key={g.group} value={g.group}>
                        {g.group === 0 ? "Main" : `Unlinked ${g.group}`} ({g.clips.length} clips)
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label>
                Frame rate
                <select value={settings.rate} onChange={(e) => update({ rate: e.target.value })} data-testid="rate">
                  <option value="">Automatic{auto ? ` (${rateLabel(auto)})` : ""}</option>
                  {RATES.map((r) => (
                    <option key={r} value={r}>
                      {rateLabel(r)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Starts at
                <input
                  value={settings.startTimecode}
                  onChange={(e) => update({ startTimecode: e.target.value })}
                  aria-invalid={!timecodeOk}
                  size={11}
                  title={
                    parseRate(settings.rate || auto) &&
                    [30000 / 1001, 60000 / 1001].includes(parseRate(settings.rate || auto)!)
                      ? "HH:MM:SS:FF, or HH:MM:SS;FF for drop-frame"
                      : "HH:MM:SS:FF"
                  }
                  data-testid="start-timecode"
                />
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={settings.includeUncertain}
                  onChange={(e) => update({ includeUncertain: e.target.checked })}
                />
                Include clips that need review{reviewCount ? ` (${reviewCount})` : ""}
              </label>
            </div>
            {!timecodeOk && <p className="error-text">Enter the start as HH:MM:SS:FF.</p>}

            <p className="muted small">Original media is referenced where it is; nothing is copied or re-encoded.</p>
            <div className="modal-actions">
              <button type="button" onClick={closeExport}>
                Cancel
              </button>
              <button type="submit" className="primary" disabled={busy || !timecodeOk} data-testid="export-submit">
                Export…
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
