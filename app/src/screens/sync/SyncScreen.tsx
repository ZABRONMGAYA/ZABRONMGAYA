// S05 Analysis (live progress of the whole production, counts and rates only: no time estimates) and S08 Sync
// results (categories, per-source table, review callout).
import { CircleCheck, Download, ListX, Pause, Play, RotateCcw, ScanEye, TriangleAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { bridge, call } from "../../api/client";
import type { PipelineStatus, ResultCategory, StageCounts, TaskRow } from "../../api/contract";
import {
  Button,
  ConfidenceBar,
  Dialog,
  Eyebrow,
  SegmentedProgress,
  SyncBadge,
  shortcut,
  statusFor,
} from "../../design-system/components";
import { formatDuration } from "../../lib/format";
import { useAnalyze } from "../../state/analyze";
import { useProd } from "../../state/production";
import { useApp } from "../../state/store";
import { CATEGORY_LABEL, deviceLetters } from "../media/labels";
import { AiSync } from "./AiSync";

export function SyncScreen() {
  const view = useProd((s) => s.syncView);
  const pipeline = useProd((s) => s.pipeline);
  const summary = useProd((s) => s.summary);
  const running = pipeline?.sync !== null && pipeline?.sync !== undefined && pipeline.sync.phase !== "done";
  const aiClip = useAnalyze((s) => s.aiClip);
  return (
    <div className="sy-sync" data-testid="sync-screen">
      <div className="sy-sync__tabs" role="tablist" aria-label="Sync views">
        <button
          type="button"
          role="tab"
          aria-selected={view === "analysis"}
          onClick={() => useProd.getState().setSyncView("analysis")}
          data-testid="sync-view-analysis"
        >
          Progress {running && <span className="sy-sync__live">running</span>}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "results"}
          onClick={() => useProd.getState().setSyncView("results")}
          disabled={!summary?.last_run}
          data-testid="sync-view-results"
        >
          Results
        </button>
        {aiClip !== null && (
          <button
            type="button"
            role="tab"
            aria-selected={view === "ai"}
            onClick={() => useProd.getState().setSyncView("ai")}
            data-testid="sync-view-ai"
          >
            AI sync
          </button>
        )}
      </div>
      {view === "ai" ? <AiSync /> : view === "results" && summary?.last_run ? <Results /> : <Analysis />}
    </div>
  );
}

// ------------------------------------------------------------------ S05

interface StageRow {
  key: string;
  label: string;
  desc: string;
  counts: StageCounts | null;
  unit: string;
  waiting?: string;
}

function stageState(c: StageCounts | null, active: boolean): { word: string; done: number; total: number } {
  if (!c) return { word: active ? "Waiting" : "Not started", done: 0, total: 0 };
  const finished = c.done + c.failed + c.skipped + c.cancelled;
  const total = finished + c.pending + c.running;
  if (total === 0) return { word: active ? "Waiting" : "Not started", done: 0, total: 0 };
  if (c.pending + c.running === 0) return { word: "Complete", done: finished, total };
  return { word: c.running ? "Working" : "Queued", done: finished, total };
}

function Analysis() {
  const pipeline = useProd((s) => s.pipeline);
  const rows = useProd((s) => s.rows);
  const devices = useProd((s) => s.devices);
  const [errors, setErrors] = useState(false);
  const cameras = devices.filter((d) => d.kind !== "recorder").length;
  const audio = devices.filter((d) => d.kind === "recorder").length;
  const sync = pipeline?.sync ?? null;
  const paused = pipeline?.state === "paused";
  const stages: StageRow[] = [
    {
      key: "probe",
      label: "Metadata",
      desc: "Reading files: format, clocks, cameras",
      counts: pipeline?.stages.probe ?? null,
      unit: "files",
    },
    {
      key: "analyze",
      label: "Audio",
      desc: "Analysis signal, waveform and fingerprint",
      counts: pipeline?.stages.analyze ?? null,
      unit: "clips",
    },
    {
      key: "match",
      label: "Matching",
      desc: sync?.strategy === "exhaustive" ? "Every pair of sources compared" : "Fingerprint candidates verified",
      counts: pipeline?.stages.match ?? null,
      unit: "pairs",
    },
    {
      key: "extend",
      label: "Extended search",
      desc: "Full search for clips still unmatched",
      counts: pipeline?.stages.extend ?? null,
      unit: "searches",
    },
  ];
  const speech = pipeline?.stages.transcribe;
  if (speech && speech.done + speech.pending + speech.running + speech.failed + speech.skipped + speech.cancelled > 0)
    stages.push({
      key: "transcribe",
      label: "Transcription",
      desc: "Speech to text and voices, on this computer",
      counts: speech,
      unit: "parts",
    });
  const failed = stages.reduce((n, s) => n + (s.counts?.failed ?? 0), 0);
  const title = paused
    ? "Paused"
    : sync && sync.phase !== "done"
      ? "Synchronizing"
      : pipeline?.state === "running"
        ? "Analyzing project"
        : "Analysis complete";
  const phaseText: Record<string, string> = {
    waiting: "Waiting for metadata and audio analysis to finish.",
    planning:
      sync?.to_query !== undefined
        ? `Finding candidate pairs: ${sync.queried ?? 0} of ${sync.to_query} clips looked up in the fingerprint index.`
        : "Finding candidate pairs.",
    matching: `Verifying ${(sync?.planned ?? 0).toLocaleString()} candidate pairs${sync?.reused ? ` (${sync.reused.toLocaleString()} already known)` : ""}.`,
    extending: `${(sync?.unmatched ?? 0).toLocaleString()} clips without a confident match get an extended search.`,
    solving: "Placing every clip on its session timeline.",
    done: sync?.elapsed_s !== undefined ? `Finished in ${formatDuration(sync.elapsed_s)}.` : "Finished.",
  };

  return (
    <div className="sy-analysis">
      <main className="sy-analysis__main">
        <Eyebrow>
          {rows.length.toLocaleString()} clips · {cameras} camera{cameras === 1 ? "" : "s"} · {audio} audio source
          {audio === 1 ? "" : "s"}
        </Eyebrow>
        <h1 className="sy-analysis__title" data-testid="analysis-title">
          {title}
        </h1>
        <p className="sy-dim sy-analysis__lead">
          {sync
            ? phaseText[sync.phase]
            : "Media is read and analysed as it is imported. Start a sync when you are ready; it waits for anything still running."}{" "}
          Counts only: Syncora does not guess how long the rest will take.
        </p>
        <div className="sy-stages" data-testid="stages">
          {stages.map((s) => {
            const active = pipeline?.state === "running";
            const st = stageState(s.counts, active);
            const complete = st.word === "Complete";
            const rate = s.counts?.rate_per_min;
            return (
              <div key={s.key} className="sy-stage" data-testid={`stage-row-${s.key}`}>
                <div>
                  <div className="sy-stage__label">{s.label}</div>
                  <div className="sy-stage__desc">{s.desc}</div>
                </div>
                <SegmentedProgress value={st.total ? st.done / st.total : 0} complete={complete} />
                <div>
                  <div className={`sy-stage__word ${complete ? "sy-stage__word--done" : ""}`}>{st.word}</div>
                  <div className="sy-stage__count tnum">
                    {st.done.toLocaleString()} / {st.total.toLocaleString()} {s.unit}
                    {rate ? ` · ${Math.round(rate).toLocaleString()}/min` : ""}
                    {s.counts?.failed ? <span className="sy-warn"> · {s.counts.failed} failed</span> : null}
                    {s.counts?.skipped ? ` · ${s.counts.skipped} skipped` : null}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        <div className="sy-analysis__actions">
          {paused ? (
            <Button
              variant="tertiary"
              size="36"
              icon={Play}
              onClick={() => void useProd.getState().resumePipeline()}
              shortcut={shortcut("⌘.")}
              data-testid="resume"
            >
              Resume
            </Button>
          ) : (
            <Button
              variant="tertiary"
              size="36"
              icon={Pause}
              onClick={() => void useProd.getState().pause()}
              shortcut={shortcut("⌘.")}
              data-testid="pause"
            >
              Pause
            </Button>
          )}
          <Button
            variant="secondary"
            size="36"
            onClick={() => useProd.getState().setStage("media")}
            shortcut={shortcut("⌘B")}
          >
            Run in background
          </Button>
          {!sync || sync.phase === "done" ? (
            <Button
              variant="primary"
              size="36"
              onClick={() => void useProd.getState().startSync()}
              data-testid="start-sync"
            >
              Sync all
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="36"
            onClick={() => void useProd.getState().cancelAll()}
            data-testid="cancel-all"
          >
            Cancel analysis
          </Button>
        </div>
        <Queue pipeline={pipeline} failed={failed} onErrors={() => setErrors(true)} />
      </main>
      <aside className="sy-analysis__aside">
        <div className="sy-section-head sy-eyebrow">By source</div>
        <SourceProgress />
        <div className="sy-section-head sy-eyebrow">Activity</div>
        <div className="sy-activity" aria-live="polite" data-testid="activity">
          {(pipeline?.activity ?? [])
            .slice()
            .reverse()
            .map((a, k) => (
              <div key={k} className={`sy-activity__line sy-activity__line--${a.level}`}>
                <span className="sy-muted">{a.time}</span> {a.what} · {a.text}
              </div>
            ))}
        </div>
      </aside>
      {errors && <ErrorsDialog onClose={() => setErrors(false)} />}
    </div>
  );
}

function Queue({
  pipeline,
  failed,
  onErrors,
}: {
  pipeline: PipelineStatus | null;
  failed: number;
  onErrors: () => void;
}) {
  if (!pipeline) return null;
  const kinds = ["probe", "analyze", "match", "extend", "transcribe"] as const;
  const total = (k: keyof StageCounts) =>
    kinds.reduce((n, kind) => n + ((pipeline.stages[kind]?.[k] as number) ?? 0), 0);
  return (
    <section className="sy-queue" data-testid="job-queue">
      <div className="sy-queue__head">
        <Eyebrow>Job queue</Eyebrow>
        <span className="sy-muted">
          Workers: {pipeline.workers.probe} metadata · {pipeline.workers.analyze} audio · {pipeline.workers.match}{" "}
          matching{pipeline.workers.speech ? ` · ${pipeline.workers.speech} speech` : ""}
        </span>
      </div>
      <div className="sy-queue__counts tnum">
        <span>
          <strong>{total("pending").toLocaleString()}</strong> pending
        </span>
        <span>
          <strong>{total("running").toLocaleString()}</strong> processing
        </span>
        <span>
          <strong>{total("done").toLocaleString()}</strong> completed
        </span>
        <span className={failed ? "sy-warn" : undefined}>
          <strong>{total("failed").toLocaleString()}</strong> failed
        </span>
        <span>
          <strong>{total("skipped").toLocaleString()}</strong> skipped
        </span>
        <span>
          <strong>{total("cancelled").toLocaleString()}</strong> cancelled
        </span>
      </div>
      <div className="sy-queue__actions">
        <Button
          variant="secondary"
          size="compact"
          icon={ListX}
          onClick={onErrors}
          disabled={failed === 0}
          data-testid="view-errors"
        >
          View errors
        </Button>
        <Button
          variant="secondary"
          size="compact"
          icon={RotateCcw}
          onClick={() => void useProd.getState().retry()}
          disabled={failed === 0}
          data-testid="retry-failed"
        >
          Retry failed
        </Button>
        <Button
          variant="secondary"
          size="compact"
          icon={X}
          onClick={() => void useProd.getState().cancelAll()}
          disabled={total("pending") === 0}
        >
          Cancel all pending
        </Button>
      </div>
      {pipeline.running.length > 0 && (
        <div className="sy-queue__running" data-testid="running-tasks">
          {pipeline.running.slice(0, 12).map((r) => (
            <div key={r.task_id} className="sy-queue__task">
              <span className="sy-queue__kind">{KIND_LABEL[r.kind]}</span>
              <span className="sy-ellipsis">{r.name}</span>
              <span className="sy-muted tnum">{r.seconds.toFixed(0)} s</span>
              <button
                type="button"
                className="sy-btn sy-btn--icon sy-btn--compact"
                aria-label={`Cancel ${r.name}`}
                title="Cancel this task"
                onClick={() => void useApp.getState().run(() => call("tasks.cancel", { ids: [r.task_id] }))}
              >
                <X size={12} aria-hidden />
              </button>
            </div>
          ))}
          {pipeline.running.length > 12 && <div className="sy-muted">and {pipeline.running.length - 12} more</div>}
        </div>
      )}
    </section>
  );
}

const KIND_LABEL: Record<string, string> = {
  probe: "Metadata",
  analyze: "Audio",
  match: "Match",
  extend: "Extended",
  transcribe: "Speech",
};

function SourceProgress() {
  const rows = useProd((s) => s.rows);
  const devices = useProd((s) => s.devices);
  const letters = useMemo(() => deviceLetters(devices), [devices]);
  const per = useMemo(() => {
    const m = new Map<number, { total: number; analysed: number; failed: number; synced: number }>();
    for (const r of rows) {
      if (r.device_id === null) continue;
      const e = m.get(r.device_id) ?? { total: 0, analysed: 0, failed: 0, synced: 0 };
      e.total++;
      if (r.analysis === "done" || r.channels === null) e.analysed++;
      if (r.analysis === "failed") e.failed++;
      if (r.category === "synchronized" || r.category === "high_confidence") e.synced++;
      m.set(r.device_id, e);
    }
    return m;
  }, [rows]);
  return (
    <div className="sy-sources">
      {devices.map((d) => {
        const e = per.get(d.id);
        if (!e) return null;
        const l = letters.get(d.id);
        const done = e.analysed >= e.total;
        return (
          <div key={d.id} className="sy-source">
            <span className={`sy-chip-letter ${l?.audio ? "sy-chip-letter--audio" : ""}`}>{l?.letter}</span>
            <div className="sy-source__body">
              <div className="sy-source__name">{d.name}</div>
              <div className={`sy-bar sy-bar--${done ? "high" : "running"}`} style={{ height: 2 }}>
                <span style={{ width: `${(e.analysed / e.total) * 100}%` }} />
              </div>
            </div>
            <span className={`sy-source__state ${e.failed ? "sy-warn" : done ? "sy-ok" : ""}`}>
              {e.failed ? `${e.failed} failed` : done ? `${e.synced}/${e.total} synced` : `${e.analysed}/${e.total}`}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function ErrorsDialog({ onClose }: { onClose: () => void }) {
  const [tasks, setTasks] = useState<TaskRow[] | null>(null);
  const load = () => void call("tasks.list", { statuses: ["failed"], limit: 500 }).then(setTasks, () => setTasks([]));
  useEffect(load, []);
  return (
    <Dialog
      title="Errors"
      onClose={onClose}
      wide
      testId="errors-dialog"
      footer={
        <>
          <span className="sy-muted">Every other file keeps being processed. Retrying reads the file again.</span>
          <span className="sy-spacer" />
          <Button variant="secondary" size="36" onClick={onClose}>
            Close
          </Button>
          <Button
            variant="primary"
            size="36"
            disabled={!tasks?.length}
            onClick={() => void useProd.getState().retry().then(load)}
          >
            Retry all
          </Button>
        </>
      }
    >
      {tasks === null ? (
        <p>Loading…</p>
      ) : tasks.length === 0 ? (
        <p>No errors.</p>
      ) : (
        <div className="sy-errors">
          {tasks.map((t) => (
            <div key={t.id} className="sy-errors__row">
              <span className="sy-queue__kind">{KIND_LABEL[t.kind]}</span>
              <span className="sy-ellipsis" title={t.name ?? t.target}>
                {(t.name ?? t.target).split(/[\\/]/).pop()}
                {t.other_name ? ` ↔ ${t.other_name}` : ""}
              </span>
              <span className="sy-mono sy-errors__msg" title={t.error ?? ""}>
                {t.error}
              </span>
              <span className="sy-muted tnum">{t.attempts}×</span>
              <Button
                variant="ghost"
                size="compact"
                onClick={() =>
                  void useApp.getState().run(async () => {
                    await call("tasks.retry", { ids: [t.id] });
                    load();
                  })
                }
              >
                Retry
              </Button>
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}

// ------------------------------------------------------------------ S08

const CATEGORY_ORDER: { key: ResultCategory; query: string }[] = [
  { key: "synchronized", query: "synchronized" },
  { key: "high_confidence", query: "confidence >= 95%" },
  { key: "review", query: "review" },
  { key: "manual", query: "manual" },
  { key: "failed", query: "failed" },
  { key: "skipped", query: "skipped" },
];

function Results() {
  const summary = useProd((s) => s.summary)!;
  const devices = useProd((s) => s.devices);
  const threshold = summary.threshold;
  const letters = useMemo(() => deviceLetters(devices), [devices]);
  const c = summary.counts;
  const cameras = summary.sources.filter((s) => s.kind !== "recorder").length;
  const audio = summary.sources.filter((s) => s.kind === "recorder").length;
  const elapsed = summary.run?.elapsed_s;
  const goto = (query: string) => {
    const prod = useProd.getState();
    prod.setBin("all");
    prod.setQuery(query);
    prod.setStage("media");
  };
  const worst = summary.sources.filter((s) => s.counts.review > 0).sort((a, b) => b.counts.review - a.counts.review)[0];

  return (
    <div className="sy-results" data-testid="results">
      <header className="sy-results__head">
        <div>
          <div className="sy-results__ok">
            <CircleCheck size={16} aria-hidden />
            {elapsed !== undefined ? `Finished in ${formatDuration(elapsed)}` : "Finished"}
          </div>
          <h1 className="sy-results__title">Sync complete</h1>
          <div className="sy-results__counts" data-testid="sync-stats">
            <span>
              {cameras} Camera{cameras === 1 ? "" : "s"}
            </span>
            <span>
              {audio} Audio Source{audio === 1 ? "" : "s"}
            </span>
            <span>{(c.synchronized ?? 0).toLocaleString()} synced</span>
            {c.review > 0 && (
              <span className="sy-warn">
                <TriangleAlert size={20} aria-hidden /> {c.review.toLocaleString()} review recommended
              </span>
            )}
          </div>
        </div>
        <div className="sy-results__actions">
          <Button variant="secondary" size="dialog" icon={Download} onClick={() => void saveReport()}>
            Save report
          </Button>
          {c.review > 0 && (
            <Button variant="warning" size="dialog" onClick={() => goto("review")} data-testid="review-clips">
              Review {c.review.toLocaleString()} clip{c.review === 1 ? "" : "s"}
            </Button>
          )}
          <Button
            variant="primary"
            size="dialog"
            onClick={() => {
              useProd.getState().setStage("timeline");
              useApp.getState().fit();
            }}
            shortcut={shortcut("⌘4")}
            data-testid="open-timeline"
          >
            Open timeline
          </Button>
        </div>
      </header>

      <div className="sy-categories" data-testid="categories">
        {CATEGORY_ORDER.map(({ key, query }) => (
          <button
            key={key}
            type="button"
            className={`sy-category sy-category--${key}`}
            onClick={() => goto(query)}
            data-testid={`category-${key}`}
          >
            <span className="sy-category__n tnum">{(c[key] ?? 0).toLocaleString()}</span>
            <span className="sy-category__label">{CATEGORY_LABEL[key]}</span>
          </button>
        ))}
      </div>

      {c.review > 0 && worst && (
        <div className="sy-callout" role="note">
          <TriangleAlert size={20} className="sy-banner__icon" aria-hidden />
          <div>
            <div className="sy-banner__title">
              Review recommended · {worst.name}
              {worst.min_confidence !== null ? ` at ${Math.round(worst.min_confidence * 100)}%` : ""}
            </div>
            <div className="sy-banner__body">
              {c.review.toLocaleString()} placement{c.review === 1 ? " is" : "s are"} below the review threshold of{" "}
              {Math.round(threshold * 100)}% or rest on recording times alone. Nothing below the threshold is locked
              automatically.
            </div>
          </div>
          <button type="button" className="sy-link" onClick={() => goto("review")}>
            Show clips
          </button>
        </div>
      )}

      {c.manual + c.failed > 0 && (
        <div className="sy-callout sy-callout--ai" role="note">
          <ScanEye size={20} className="sy-banner__icon" aria-hidden />
          <div>
            <div className="sy-banner__title">
              {(c.manual + c.failed).toLocaleString()} clip{c.manual + c.failed === 1 ? "" : "s"} could not be placed by
              audio
            </div>
            <div className="sy-banner__body">
              AI sync looks for them by what was said, flashes and light changes, and the camera clocks. Its proposals
              are shown for review; nothing is locked automatically.
            </div>
          </div>
          <Button
            variant="secondary"
            size="36"
            icon={ScanEye}
            onClick={() =>
              void useApp.getState().run(async () => {
                await call("ai.fallback", {});
                useApp.getState().toast("info", "AI sync is looking for the clips audio could not place.");
              })
            }
            data-testid="ai-fallback"
          >
            Find them with AI
          </Button>
        </div>
      )}

      <div className="sy-results__table" role="table" aria-label="Results by source">
        <div className="sy-results__row sy-results__row--head" role="row">
          <span />
          <span>Source</span>
          <span>Confidence (median)</span>
          <span />
          <span>Clips synced</span>
          <span>Needs attention</span>
          <span>Status</span>
        </div>
        {summary.sources.map((s) => {
          // Nothing of this source could be placed by audio: its clips wait for manual sync. Their clock-only
          // estimate is not a sync confidence, so none is shown.
          const manualOnly = s.counts.synchronized === 0 && s.counts.review === 0 && s.counts.manual > 0;
          const conf = manualOnly ? null : s.median_confidence;
          const status = conf === null ? "none" : statusFor(s.min_confidence ?? conf, threshold);
          const attention = s.counts.review + s.counts.manual + s.counts.failed;
          const l = s.device_id !== null ? letters.get(s.device_id) : undefined;
          return (
            <div
              key={s.device_id ?? "none"}
              role="row"
              className={`sy-results__row ${s.counts.review ? "sy-results__row--review" : ""}`}
              onClick={() => {
                const prod = useProd.getState();
                if (s.device_id !== null) prod.setBin(`device:${s.device_id}`);
                prod.setQuery("");
                prod.setStage("media");
              }}
            >
              <span className={`sy-chip-letter ${l?.audio ? "sy-chip-letter--audio" : ""}`}>{l?.letter ?? "–"}</span>
              <span className="sy-results__name sy-ellipsis">{s.name}</span>
              <span>
                {conf !== null && <ConfidenceBar value={conf} tone={status === "none" ? "good" : status} height={8} />}
              </span>
              <span className="sy-results__pct tnum">{conf !== null ? `${Math.round(conf * 100)}%` : "—"}</span>
              <span className="tnum">
                {s.counts.synchronized.toLocaleString()} of {s.clips.toLocaleString()}
              </span>
              <span className="tnum">
                {attention
                  ? `${attention.toLocaleString()} (${[
                      s.counts.review && `${s.counts.review} review`,
                      s.counts.manual && `${s.counts.manual} manual`,
                      s.counts.failed && `${s.counts.failed} failed`,
                    ]
                      .filter(Boolean)
                      .join(", ")})`
                  : "—"}
              </span>
              <span>
                <SyncBadge
                  status={status === "none" ? "none" : status}
                  confidence={manualOnly ? undefined : s.min_confidence}
                  size="md"
                  label={manualOnly ? "Manual sync required" : status === "review" ? "Review recommended" : undefined}
                />
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

async function saveReport(): Promise<void> {
  const app = useApp.getState();
  const project = app.project;
  if (!project) return;
  await app.run(async () => {
    const [summary, index] = await Promise.all([call("sync.summary", {}), call("media.index", {})]);
    const report = { project: project.name, generated: new Date().toISOString(), summary, clips: index };
    const where = await bridge().saveReport(`${project.name} sync report`, JSON.stringify(report, null, 2));
    if (where) app.toast("success", "Report saved.");
  });
}
