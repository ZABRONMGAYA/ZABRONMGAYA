// Stage 03 Analyze: Overview (transcription progress), Transcript (S09), Speakers, Markers and Search (S10).
// Everything runs on this computer with the models shipped with Syncora; nothing is uploaded.
import { Captions, Download, Play, RotateCcw, X } from "lucide-react";
import { useEffect, useMemo } from "react";

import { call } from "../../api/client";
import type { AiStatus, TranscriptClip } from "../../api/contract";
import { Button, EmptyState, Eyebrow, SegmentedProgress, Select, SyncBadge } from "../../design-system/components";
import { formatDuration } from "../../lib/format";
import { type AnalyzeTab, useAnalyze } from "../../state/analyze";
import { useProd } from "../../state/production";
import { useApp } from "../../state/store";
import { formatBytes } from "../media/labels";
import { MarkersView } from "./MarkersView";
import { SearchView } from "./SearchView";
import { SpeakersView } from "./SpeakersView";
import { TranscriptView } from "./TranscriptView";

const TABS: { id: AnalyzeTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "transcript", label: "Transcript" },
  { id: "speakers", label: "Speakers" },
  { id: "markers", label: "Markers" },
  { id: "search", label: "Search" },
];

export function AnalyzeScreen() {
  const tab = useAnalyze((s) => s.tab);
  const status = useAnalyze((s) => s.status);
  useEffect(() => {
    const a = useAnalyze.getState();
    if (!a.status) void a.loadStatus();
    void a.loadOverview();
    void a.loadSpeakers();
  }, []);
  return (
    <div className="sy-analyze" data-testid="analyze-screen">
      <div className="sy-sync__tabs" role="tablist" aria-label="Analyze views">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => useAnalyze.getState().setTab(t.id)}
            data-testid={`analyze-tab-${t.id}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {status && !status.ready && <AiProblem status={status} />}
      {tab === "overview" && <Overview />}
      {tab === "transcript" && <TranscriptView />}
      {tab === "speakers" && <SpeakersView />}
      {tab === "markers" && <MarkersView />}
      {tab === "search" && <SearchView />}
    </div>
  );
}

function AiProblem({ status }: { status: AiStatus }) {
  return (
    <div className="sy-banner sy-analyze__problem" role="alert">
      <div>
        <div className="sy-banner__title">Transcription is not available</div>
        <div className="sy-banner__body">
          {status.problem ?? "No speech model is installed."} Download a speech model in Settings → AI.
        </div>
      </div>
      <Button variant="secondary" size="compact" onClick={() => useProd.getState().openSettings("ai")}>
        Open AI settings
      </Button>
    </div>
  );
}

function languageMix(languages: Record<string, number>, names: Map<string, string>): string {
  const total = Object.values(languages).reduce((a, b) => a + b, 0);
  if (!total) return "";
  return Object.entries(languages)
    .slice(0, 3)
    .map(([code, n]) => `${names.get(code) ?? code.toUpperCase()} ${Math.round((n / total) * 100)}%`)
    .join(" · ");
}

const CLIP_STATUS: Record<
  TranscriptClip["status"],
  { badge: "high" | "running" | "none" | "review" | "failed"; label: string }
> = {
  done: { badge: "high", label: "Transcribed" },
  running: { badge: "running", label: "Transcribing" },
  queued: { badge: "none", label: "Queued" },
  partial: { badge: "review", label: "Partly failed" },
  failed: { badge: "failed", label: "Failed" },
};

function Overview() {
  const status = useAnalyze((s) => s.status);
  const overview = useAnalyze((s) => s.overview);
  const speakers = useAnalyze((s) => s.speakers);
  const markers = useAnalyze((s) => s.markers);
  const project = useApp((s) => s.project);
  const rows = useProd((s) => s.rows);
  const speech = useProd((s) => s.pipeline?.stages.transcribe);
  const names = useMemo(() => new Map(status?.languages.map((l) => [l.code, l.name]) ?? []), [status]);
  const withSound = rows.filter((r) => r.channels !== null && r.channels > 0).length;
  const settings = project?.settings;
  const model = status?.models.find((m) => m.id === settings?.transcription_model);
  const downloading = model?.download && !model.download.error;

  const chunksDone = (speech?.done ?? 0) + (speech?.skipped ?? 0);
  const chunksTotal = chunksDone + (speech?.pending ?? 0) + (speech?.running ?? 0) + (speech?.failed ?? 0);
  const active = (speech?.pending ?? 0) + (speech?.running ?? 0) > 0;
  const totals = overview?.totals;
  const clips = overview?.clips ?? [];
  const sound = markers.filter((m) => m.source === "speech").length;

  const update = (patch: Parameters<ReturnType<typeof useApp.getState>["updateSettings"]>[0]) =>
    void useApp.getState().updateSettings(patch);

  return (
    <div className="sy-analysis sy-analyze__overview">
      <main className="sy-analysis__main">
        <Eyebrow>
          {withSound.toLocaleString()} clips with sound · {totals?.clips ?? 0} transcribed ·{" "}
          {formatDuration(totals?.speech_s ?? 0)} of speech
        </Eyebrow>
        <h1 className="sy-analysis__title">{active ? "Transcribing" : "Analyze"}</h1>
        <p className="sy-dim sy-analysis__lead">
          Syncora transcribes what is said, tells the speakers apart by their voices and marks applause, music and
          laughter. It runs on this computer with the models included in Syncora: nothing is uploaded. Transcripts make
          speech searchable, and let AI sync place clips audio fingerprints could not.
        </p>
        <div className="sy-stages" data-testid="analyze-stages">
          <div className="sy-stage">
            <div>
              <div className="sy-stage__label">Transcription</div>
              <div className="sy-stage__desc">Speech to text, 10-minute parts</div>
            </div>
            <SegmentedProgress
              value={chunksTotal ? chunksDone / chunksTotal : totals?.clips ? 1 : 0}
              complete={!active && chunksTotal > 0}
            />
            <div>
              <div className={`sy-stage__word ${!active && chunksTotal ? "sy-stage__word--done" : ""}`}>
                {active ? "Working" : chunksTotal || totals?.clips ? "Complete" : "Not started"}
              </div>
              <div className="sy-stage__count tnum">
                {chunksTotal ? `${chunksDone} / ${chunksTotal} parts` : `${totals?.clips ?? 0} clips`}
                {speech?.failed ? <span className="sy-warn"> · {speech.failed} failed</span> : null}
              </div>
            </div>
          </div>
          <div className="sy-stage">
            <div>
              <div className="sy-stage__label">Speakers</div>
              <div className="sy-stage__desc">Voices told apart across every recording</div>
            </div>
            <SegmentedProgress value={speakers.length ? 1 : 0} complete={speakers.length > 0 && !active} />
            <div>
              <div className="sy-stage__word">{speakers.length} detected</div>
              <div className="sy-stage__count tnum">
                {totals?.languages ? languageMix(totals.languages, names) || "—" : "—"}
              </div>
            </div>
          </div>
          <div className="sy-stage">
            <div>
              <div className="sy-stage__label">Moments</div>
              <div className="sy-stage__desc">Applause, music, laughter and your markers</div>
            </div>
            <SegmentedProgress value={markers.length ? 1 : 0} complete={markers.length > 0 && !active} />
            <div>
              <div className="sy-stage__word">{markers.length} markers</div>
              <div className="sy-stage__count tnum">
                {sound} heard · {markers.length - sound} added
              </div>
            </div>
          </div>
        </div>

        <div className="sy-analysis__actions">
          <Button
            variant="primary"
            size="36"
            icon={Captions}
            disabled={!status?.ready || !model?.installed}
            onClick={() => void useAnalyze.getState().transcribe("smart")}
            title="Each moment once, from its clearest recording (audio recorders first)"
            data-testid="transcribe-project"
          >
            Transcribe project
          </Button>
          <Button
            variant="secondary"
            size="36"
            disabled={!status?.ready || !model?.installed}
            onClick={() => void useAnalyze.getState().transcribe("all")}
            title="Every clip with sound, even when another recording heard the same moment"
          >
            Transcribe every clip
          </Button>
          {active && (
            <Button variant="ghost" size="36" icon={X} onClick={() => void useAnalyze.getState().cancelTranscription()}>
              Cancel transcription
            </Button>
          )}
        </div>

        {settings && status && (
          <div className="sy-analyze__settings">
            <label>
              <span>Speech model</span>
              <Select
                label="Speech model"
                value={settings.transcription_model}
                width={300}
                onChange={(v) => update({ transcription_model: v })}
                options={status.models
                  .filter((m) => m.kind === "speech")
                  .map((m) => ({
                    value: m.id,
                    label: `${m.title} · ${m.detail}${m.installed ? "" : ` (download ${formatBytes(m.download_bytes)})`}`,
                  }))}
              />
            </label>
            <label>
              <span>Language</span>
              <Select
                label="Language"
                value={settings.transcription_language}
                width={220}
                onChange={(v) => update({ transcription_language: v })}
                options={[
                  { value: "auto", label: "Auto detect" },
                  ...status.languages.map((l) => ({ value: l.code, label: l.name })),
                ]}
              />
            </label>
            {model && !model.installed && (
              <Button
                variant="secondary"
                size="36"
                icon={Download}
                loading={Boolean(downloading)}
                loadingLabel={
                  model.download
                    ? `Downloading ${formatBytes(model.download.received)} of ${formatBytes(model.download.total)}`
                    : "Downloading…"
                }
                onClick={() =>
                  void useApp.getState().run(async () => {
                    await call("ai.download_model", { model_id: model.id });
                    await useAnalyze.getState().loadStatus();
                  })
                }
              >
                Download {model.title} ({formatBytes(model.download_bytes)})
              </Button>
            )}
          </div>
        )}

        {clips.length === 0 ? (
          <EmptyState
            icon={Captions}
            title="No transcript"
            body="Transcription hasn’t run for this project. It runs on device."
            action={
              <Button
                variant="secondary"
                size="compact"
                disabled={!status?.ready || !model?.installed}
                onClick={() => void useAnalyze.getState().transcribe("smart")}
              >
                Transcribe
              </Button>
            }
          />
        ) : (
          <section className="sy-analyze__clips" data-testid="transcript-clips">
            <div className="sy-analyze__clip sy-analyze__clip--head">
              <span>Recording</span>
              <span>Status</span>
              <span>Parts</span>
              <span>Language</span>
              <span />
            </div>
            {clips.map((c) => {
              const st = CLIP_STATUS[c.status];
              return (
                <div key={c.clip_id} className="sy-analyze__clip">
                  <button
                    type="button"
                    className="sy-link sy-ellipsis"
                    onClick={() => void useAnalyze.getState().openClip(c.clip_id)}
                    title={`Open the transcript of ${c.name}`}
                  >
                    {c.name}
                    {c.device_name && <span className="sy-muted"> · {c.device_name}</span>}
                  </button>
                  <SyncBadge status={st.badge} label={st.label} />
                  <span className="tnum">
                    {c.done} / {c.chunks}
                  </span>
                  <span>{c.language === "auto" ? "Auto" : (names.get(c.language) ?? c.language)}</span>
                  <span className="sy-analyze__clip-actions">
                    <Button
                      variant="ghost"
                      size="compact"
                      icon={Play}
                      onClick={() => void useAnalyze.getState().openClip(c.clip_id)}
                    >
                      Open
                    </Button>
                    {c.status === "running" || c.status === "queued" ? (
                      <Button
                        variant="ghost"
                        size="compact"
                        icon={X}
                        onClick={() => void useAnalyze.getState().cancelTranscription([c.clip_id])}
                      >
                        Cancel
                      </Button>
                    ) : (
                      <Button
                        variant="ghost"
                        size="compact"
                        icon={RotateCcw}
                        title="Transcribe again with the current model and language"
                        onClick={() => void useAnalyze.getState().transcribe("clips", [c.clip_id], true)}
                      >
                        Redo
                      </Button>
                    )}
                  </span>
                </div>
              );
            })}
          </section>
        )}
      </main>
      <aside className="sy-analysis__aside">
        <div className="sy-section-head sy-eyebrow">On this computer</div>
        <div className="sy-analyze__facts">
          <dl className="sy-kv">
            <dt>Speech</dt>
            <dd>{model ? `${model.title} (${model.detail})` : "…"}</dd>
            <dt>Voices</dt>
            <dd>3D-Speaker ERes2Net voice fingerprints</dd>
            <dt>Detection</dt>
            <dd>Silero voice activity detector</dd>
            <dt>Uploads</dt>
            <dd>None: models and media stay on this computer</dd>
          </dl>
        </div>
        <div className="sy-section-head sy-eyebrow">Tips</div>
        <ul className="sy-analyze__tips">
          <li>“Transcribe project” transcribes each moment once, from its clearest recording.</li>
          <li>A camera shows what the recorder heard at the same moment, once both are synced.</li>
          <li>Rename a speaker once: every segment, marker and search result follows.</li>
          <li>Press M in the transcript to mark a moment; ⌘K searches everything.</li>
        </ul>
      </aside>
    </div>
  );
}
