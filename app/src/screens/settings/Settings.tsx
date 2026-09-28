// S13 Settings: Synchronization (as designed) and every other category in the same SettingRow layout.
import { Download, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";

import { call } from "../../api/client";
import type { AiModel, CacheInfo, SystemResources, WorkerPlan } from "../../api/contract";
import { Button, Segmented, Select, SettingRow, Toggle, shortcut } from "../../design-system/components";
import {
  loadSettings as loadExportDefaults,
  saveSettings as saveExportDefaults,
} from "../../features/export/ExportDialog";
import { useAnalyze } from "../../state/analyze";
import { type SettingsCategory, useProd } from "../../state/production";
import { useApp } from "../../state/store";
import { formatBytes } from "../media/labels";

// DESIGN-OPEN: #10 only the Synchronization pane is drawn; the other panes follow its layout (DESIGN_DECISIONS.md D-10).
const CATEGORIES: { id: SettingsCategory; label: string }[] = [
  { id: "general", label: "General" },
  { id: "appearance", label: "Appearance" },
  { id: "performance", label: "Performance" },
  { id: "ai", label: "AI" },
  { id: "transcription", label: "Transcription" },
  { id: "synchronization", label: "Synchronization" },
  { id: "media", label: "Media" },
  { id: "proxy", label: "Proxy" },
  { id: "export", label: "Export" },
  { id: "shortcuts", label: "Keyboard shortcuts" },
  { id: "storage", label: "Storage" },
  { id: "privacy", label: "Privacy" },
  { id: "updates", label: "Updates" },
  { id: "about", label: "About" },
];

export const THEME_KEY = "syncora.theme";
export type Theme = "dark" | "light" | "system";

export function savedTheme(): Theme {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === "light" || t === "system" ? t : "dark";
  } catch {
    return "dark";
  }
}

/** Apply a theme to the window (the timeline keeps its dark canvas, as in editing applications). */
export function applyTheme(theme: Theme): void {
  const light =
    theme === "light" || (theme === "system" && window.matchMedia?.("(prefers-color-scheme: light)").matches);
  document.documentElement.dataset.theme = light ? "light" : "dark";
}

export const WORKERS_KEY = "syncora.workers";

export function savedWorkers(): "auto" | Partial<Omit<WorkerPlan, "reason">> {
  try {
    const raw = localStorage.getItem(WORKERS_KEY);
    return raw ? (JSON.parse(raw) as "auto" | Partial<Omit<WorkerPlan, "reason">>) : "auto";
  } catch {
    return "auto";
  }
}

export function Settings() {
  const category = useProd((s) => s.settings);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") useProd.getState().closeSettings();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  if (!category) return null;
  return (
    <div className="sy-settings" role="dialog" aria-modal="true" aria-label="Settings" data-testid="settings">
      <aside className="sy-settings__nav">
        <div className="sy-settings__title">Settings</div>
        {CATEGORIES.map((c) => (
          <button
            key={c.label}
            type="button"
            className="sy-settings__item"
            aria-current={c.id === category ? "page" : undefined}
            onClick={() => useProd.getState().openSettings(c.id)}
            data-testid={`settings-${c.id}`}
          >
            {c.label}
          </button>
        ))}
      </aside>
      <main className="sy-settings__pane">
        <button
          type="button"
          className="sy-btn sy-btn--icon sy-settings__close"
          aria-label="Close settings"
          onClick={() => useProd.getState().closeSettings()}
        >
          <X size={14} aria-hidden />
        </button>
        {category === "synchronization" && <SyncPane />}
        {category === "performance" && <PerformancePane />}
        {category === "storage" && <StoragePane />}
        {category === "general" && <GeneralPane />}
        {category === "about" && <AboutPane />}
        {category === "appearance" && <AppearancePane />}
        {category === "ai" && <AiPane />}
        {category === "transcription" && <TranscriptionPane />}
        {category === "media" && <MediaPane />}
        {category === "proxy" && <ProxyPane />}
        {category === "export" && <ExportPane />}
        {category === "shortcuts" && <ShortcutsPane />}
        {category === "privacy" && <PrivacyPane />}
        {category === "updates" && <UpdatesPane />}
      </main>
    </div>
  );
}

function PaneHead({ title, sub }: { title: string; sub: string }) {
  return (
    <>
      <h1 className="sy-settings__h1">{title}</h1>
      <p className="sy-settings__sub">{sub}</p>
    </>
  );
}

function SyncPane() {
  const project = useApp((s) => s.project);
  const update = useApp((s) => s.updateSettings);
  if (!project) {
    return (
      <>
        <PaneHead title="Synchronization" sub="Open a project to change how it is synchronised." />
      </>
    );
  }
  const s = project.settings;
  const threshold = Math.round(s.review_threshold * 100);
  return (
    <>
      <PaneHead title="Synchronization" sub={`How ${project.name} is synchronised. Changes apply to the next sync.`} />
      <SettingRow
        title="Default method"
        description="Audio + clocks uses timecode and recording times to guide the audio search and to place clips without usable audio."
      >
        <Segmented
          label="Default method"
          variant="method"
          value={s.mode}
          onChange={(mode) => void update({ mode })}
          options={[
            { value: "timecode", label: "1 Timecode" },
            { value: "hybrid", label: "2 Audio + clocks" },
            { value: "audio", label: "3 Audio only" },
          ]}
        />
      </SettingRow>
      <SettingRow
        title="Use camera timecode when available"
        description="Treat every device's timecode as jam-synced (one shared clock). Leave off when cameras were not jammed together."
      >
        <Toggle
          label="Timecode is jam-synced"
          on={s.timecode_jam_synced}
          onChange={(on) => void update({ timecode_jam_synced: on })}
        />
      </SettingRow>
      <SettingRow
        title="Use recording times"
        description="Place clips without usable audio by their camera's recording time (shown as review)."
      >
        <Toggle
          label="Use recording times"
          on={s.use_creation_time}
          onChange={(on) => void update({ use_creation_time: on })}
        />
      </SettingRow>
      <SettingRow
        title="Drift correction"
        description="Clock drift between devices is measured on every match and corrected. Always on."
      >
        <Toggle label="Drift correction" on disabled onChange={() => undefined} />
      </SettingRow>
      <SettingRow
        title="AI visual + speech fallback"
        description="After each sync, clips audio could not place are looked for by what was said, flashes and light changes and the camera clocks. They are proposed as REVIEW, never locked automatically."
      >
        <Toggle label="AI fallback" on={s.ai_fallback} onChange={(on) => void update({ ai_fallback: on })} />
      </SettingRow>
      <SettingRow
        title="Search window"
        description="Audio fingerprints find overlapping clips anywhere in the production, whatever the camera clocks say."
      >
        <span className="sy-dim">Whole production</span>
      </SettingRow>
      <SettingRow
        title="Review threshold"
        description="Placements below this confidence are marked REVIEW and never locked automatically."
      >
        <input
          type="range"
          className="sy-slider"
          min={50}
          max={99}
          value={threshold}
          aria-label="Review threshold"
          onChange={(e) => void update({ review_threshold: Number(e.target.value) / 100 })}
          onMouseUp={() => void useProd.getState().loadSummary()}
        />
        <span className="tnum sy-settings__value">{threshold}%</span>
      </SettingRow>
    </>
  );
}

function PerformancePane() {
  const [res, setRes] = useState<SystemResources | null>(null);
  const [speed, setSpeed] = useState<number | null>(null);
  const [testing, setTesting] = useState(false);
  const load = () => void call("system.resources", {}).then(setRes, () => undefined);
  useEffect(load, []);
  if (!res) return <PaneHead title="Performance" sub="Detecting this computer…" />;
  const r = res.resources;
  const manual = res.mode === "manual";
  const configure = async (workers: "auto" | Partial<Omit<WorkerPlan, "reason">>) => {
    await useApp.getState().run(async () => {
      await call("engine.configure", { workers });
      try {
        localStorage.setItem(WORKERS_KEY, JSON.stringify(workers));
      } catch {
        // settings stay for this session only
      }
      load();
    });
  };
  const count = (key: "probe" | "analyze" | "match", label: string, help: string) => (
    <SettingRow title={label} description={help}>
      <input
        type="number"
        className="sy-input sy-settings__number"
        min={1}
        max={64}
        value={res.plan[key]}
        disabled={!manual}
        aria-label={label}
        onChange={(e) =>
          void configure({ ...res.plan, [key]: Math.max(1, Number(e.target.value) || 1), reason: undefined } as never)
        }
      />
      <span className="sy-muted">recommended {res.recommended[key]}</span>
    </SettingRow>
  );
  return (
    <>
      <PaneHead
        title="Performance"
        sub="Syncora sizes its workers for this computer. Everything runs in the background; the interface stays responsive."
      />
      <div className="sy-kv sy-settings__facts">
        <dt>Processor</dt>
        <dd>
          {r.cpu_usable} cores available ({r.cpu_logical} logical)
        </dd>
        <dt>Memory</dt>
        <dd>
          {r.ram_total_bytes ? formatBytes(r.ram_total_bytes) : "unknown"}
          {r.ram_available_bytes ? ` · ${formatBytes(r.ram_available_bytes)} available` : ""}
        </dd>
        <dt>Graphics</dt>
        <dd>
          {r.gpu ?? "not detected"}
          {r.gpu_memory_bytes ? ` · ${formatBytes(r.gpu_memory_bytes)}` : ""} · not used (analysis runs on the
          processor)
        </dd>
        <dt>Cache drive</dt>
        <dd>
          {formatBytes(res.storage.cache.free_bytes)} free of {formatBytes(res.storage.cache.total_bytes)}
          {speed !== null && ` · writes ${formatBytes(speed)}/s`}
        </dd>
        {res.storage.project && (
          <>
            <dt>Project drive</dt>
            <dd>
              {formatBytes(res.storage.project.free_bytes)} free of {formatBytes(res.storage.project.total_bytes)}
            </dd>
          </>
        )}
      </div>
      <SettingRow title="Workers" description={`Auto: ${res.recommended.reason}.`}>
        <Segmented
          label="Worker mode"
          value={res.mode}
          onChange={(mode) =>
            void configure(
              mode === "auto" ? "auto" : { probe: res.plan.probe, analyze: res.plan.analyze, match: res.plan.match },
            )
          }
          options={[
            { value: "auto", label: "Auto" },
            { value: "manual", label: "Manual" },
          ]}
        />
      </SettingRow>
      {count("probe", "Metadata workers", "Files read at once (disk-bound: fewer on spinning disks).")}
      {count("analyze", "Audio workers", "Recordings decoded at once (one FFmpeg process each).")}
      {count(
        "match",
        "Matching workers",
        "Processes comparing recordings (processor-bound, about 400 MB of memory each).",
      )}
      <SettingRow
        title="Disk speed"
        description="Write speed of the drive holding the analysis cache (writes and deletes 64 MB)."
      >
        <Button
          variant="secondary"
          size="compact"
          loading={testing}
          loadingLabel="Measuring…"
          onClick={() => {
            setTesting(true);
            void call("system.disk_speed", {})
              .then((r) => setSpeed(r.write_bytes_per_s))
              .finally(() => setTesting(false));
          }}
        >
          Measure
        </Button>
      </SettingRow>
    </>
  );
}

function StoragePane() {
  const [cache, setCache] = useState<CacheInfo | null>(null);
  const [db, setDb] = useState<{ bytes: number; wal_bytes: number; rows: Record<string, number> } | null>(null);
  const project = useApp((s) => s.project);
  useEffect(() => {
    void call("cache.info", {}).then(setCache, () => undefined);
    if (project) void call("project.stats", {}).then(setDb, () => undefined);
  }, [project]);
  return (
    <>
      <PaneHead
        title="Storage"
        sub="Analysis results are cached by content, so moving or renaming media never repeats work."
      />
      <div className="sy-kv sy-settings__facts">
        <dt>Cache folder</dt>
        <dd className="sy-mono">{cache?.root ?? "…"}</dd>
        <dt>Cache size</dt>
        <dd>
          {cache ? `${formatBytes(cache.bytes)} in ${cache.entries.toLocaleString()} recordings` : "…"}
          {cache && project ? ` · ${formatBytes(cache.project_bytes)} used by this project` : ""}
        </dd>
        <dt>Free space</dt>
        <dd>{cache ? formatBytes(cache.disk.free_bytes) : "…"}</dd>
        {db && (
          <>
            <dt>Project file</dt>
            <dd>
              {formatBytes(db.bytes + db.wal_bytes)} · {db.rows.media_file?.toLocaleString()} media ·{" "}
              {db.rows.pair_match?.toLocaleString()} matches
            </dd>
          </>
        )}
      </div>
      <SettingRow
        title="Clear unused analysis"
        description="Deletes cached analysis the open project does not use. It is recomputed if another project needs it."
      >
        <Button
          variant="secondary"
          size="compact"
          onClick={() =>
            void useApp.getState().run(async () => {
              const result = await call("cache.clear_unused", {});
              setCache(result);
              useApp.getState().toast("success", `Freed ${formatBytes(result.freed_bytes)}.`);
            })
          }
        >
          Clear unused
        </Button>
      </SettingRow>
    </>
  );
}

function GeneralPane() {
  return (
    <>
      <PaneHead title="General" sub="Syncora reads media in place and never modifies or deletes original files." />
      <SettingRow
        title="Project files"
        description="Projects are saved continuously as .syncora files. Multicam Sync .mcsync projects open and are upgraded."
      >
        <span className="sy-dim">Saved automatically</span>
      </SettingRow>
      <SettingRow
        title="Resume work"
        description="Work left unfinished when a project was closed is offered for resuming when it opens again."
      >
        <span className="sy-dim">Always</span>
      </SettingRow>
    </>
  );
}

// DESIGN-OPEN: #12 About is drawn light in the Brand Kit; here it follows the app theme (DESIGN_DECISIONS.md D-10).
function AboutPane() {
  const hello = useApp((s) => s.engine.hello);
  return (
    <>
      <PaneHead title="About" sub="Syncora: automatic synchronisation of multicamera video and external audio." />
      <div className="sy-kv sy-settings__facts">
        <dt>Version</dt>
        <dd>{hello?.version ?? "…"}</dd>
        <dt>Engine</dt>
        <dd>Protocol {hello?.protocol ?? "…"}</dd>
        <dt>FFmpeg</dt>
        <dd>{hello?.ffmpeg ?? "not found"} (LGPL 2.1+)</dd>
        <dt>Typeface</dt>
        <dd>Archivo (SIL Open Font License)</dd>
        <dt>Icons</dt>
        <dd>Lucide (ISC licence)</dd>
      </div>
    </>
  );
}

function ModelRow({ model }: { model: AiModel }) {
  const download = model.download;
  const busy = Boolean(download && !download.error && !model.installed);
  return (
    <SettingRow
      title={`${model.title} · ${model.detail}`}
      description={
        model.installed
          ? model.location === "bundled"
            ? "Included with Syncora."
            : "Downloaded to this computer."
          : download?.error
            ? `Download failed: ${download.error}`
            : `${formatBytes(model.download_bytes)} download, then it works offline.`
      }
    >
      {model.installed ? (
        model.bundled ? (
          <span className="sy-ok">Installed</span>
        ) : (
          <Button
            variant="ghost"
            size="compact"
            icon={Trash2}
            onClick={() =>
              void useApp.getState().run(async () => {
                await call("ai.remove_model", { model_id: model.id });
                await useAnalyze.getState().loadStatus();
              })
            }
          >
            Remove
          </Button>
        )
      ) : (
        <Button
          variant="secondary"
          size="compact"
          icon={Download}
          loading={busy}
          loadingLabel={
            download ? `${formatBytes(download.received)} of ${formatBytes(download.total)}` : "Downloading…"
          }
          onClick={() =>
            void useApp.getState().run(async () => {
              await call("ai.download_model", { model_id: model.id });
              await useAnalyze.getState().loadStatus();
            })
          }
          data-testid={`download-${model.id}`}
        >
          Download
        </Button>
      )}
    </SettingRow>
  );
}

function useAiStatus() {
  const status = useAnalyze((s) => s.status);
  useEffect(() => {
    void useAnalyze.getState().loadStatus();
    // Downloads report their progress through the status: refresh it while one runs.
    const id = setInterval(() => {
      const st = useAnalyze.getState().status;
      if (st?.models.some((m) => m.download && !m.download.error && !m.installed))
        void useAnalyze.getState().loadStatus();
    }, 1000);
    return () => clearInterval(id);
  }, []);
  return status;
}

function AiPane() {
  const status = useAiStatus();
  const project = useApp((s) => s.project);
  const update = useApp((s) => s.updateSettings);
  return (
    <>
      <PaneHead
        title="AI"
        sub="Transcription, speaker recognition, search and AI sync run on this computer. Nothing is uploaded."
      />
      <div className="sy-kv sy-settings__facts">
        <dt>Speech engine</dt>
        <dd>
          {status ? (status.speech_engine ? "sherpa-onnx (ONNX Runtime), on the processor" : "not installed") : "…"}
        </dd>
        <dt>Status</dt>
        <dd className={status?.ready ? "sy-ok" : "sy-warn"}>
          {status ? (status.ready ? "Ready" : (status.problem ?? "No speech model installed")) : "…"}
        </dd>
      </div>
      <SettingRow
        title="AI visual + speech fallback"
        description={
          project
            ? "After each sync, look for clips audio could not place from speech, light changes and camera clocks. Results are proposed for review."
            : "Open a project to change this."
        }
      >
        <Toggle
          label="AI fallback"
          on={project?.settings.ai_fallback ?? false}
          disabled={!project}
          onChange={(on) => void update({ ai_fallback: on })}
        />
      </SettingRow>
      <h2 className="sy-settings__h2">Speech models</h2>
      {status?.models
        .filter((m) => m.kind === "speech")
        .map((m) => (
          <ModelRow key={m.id} model={m} />
        ))}
      <h2 className="sy-settings__h2">Helpers</h2>
      {status?.models
        .filter((m) => m.kind !== "speech")
        .map((m) => (
          <ModelRow key={m.id} model={m} />
        ))}
    </>
  );
}

function TranscriptionPane() {
  const status = useAiStatus();
  const project = useApp((s) => s.project);
  const update = useApp((s) => s.updateSettings);
  if (!project) return <PaneHead title="Transcription" sub="Open a project to choose how it is transcribed." />;
  const s = project.settings;
  const speech = status?.models.filter((m) => m.kind === "speech") ?? [];
  const chosen = speech.find((m) => m.id === s.transcription_model);
  return (
    <>
      <PaneHead
        title="Transcription"
        sub={`How ${project.name} is transcribed. Changes apply to the next transcription; finished transcripts are kept.`}
      />
      <SettingRow
        title="Speech model"
        description={
          chosen && !chosen.installed
            ? `${chosen.title} is not on this computer yet: download it in Settings → AI.`
            : "Larger models are more accurate and slower. Balanced suits most productions."
        }
      >
        <Select
          label="Speech model"
          value={s.transcription_model}
          width={320}
          onChange={(v) => void update({ transcription_model: v })}
          options={speech.map((m) => ({
            value: m.id,
            label: `${m.title} · ${m.detail}${m.installed ? "" : " (not downloaded)"}`,
          }))}
        />
      </SettingRow>
      <SettingRow
        title="Language"
        description="Auto detects the language of every sentence, for productions in several languages. Choose one when all speech is in it: it is faster and more accurate."
      >
        <Select
          label="Language"
          value={s.transcription_language}
          width={220}
          onChange={(v) => void update({ transcription_language: v })}
          options={[
            { value: "auto", label: "Auto detect" },
            ...(status?.languages ?? []).map((l) => ({ value: l.code, label: l.name })),
          ]}
        />
      </SettingRow>
      <SettingRow
        title="Speakers"
        description="Voices are told apart by a voice fingerprint of every sentence longer than a second, across all recordings of the project."
      >
        <span className="sy-dim">Always on</span>
      </SettingRow>
      <SettingRow
        title="Sound events"
        description="Applause, music, laughter, cheering and singing heard while transcribing become markers."
      >
        <span className="sy-dim">Always on</span>
      </SettingRow>
      <SettingRow
        title="Transcribe now"
        description="Each moment once, from its clearest recording (audio recorders first)."
      >
        <Button
          variant="secondary"
          size="compact"
          disabled={!status?.ready || !chosen?.installed}
          onClick={() => {
            useProd.getState().closeSettings();
            useProd.getState().setStage("analyze");
            useAnalyze.getState().setTab("overview");
            void useAnalyze.getState().transcribe("smart");
          }}
        >
          Transcribe project
        </Button>
      </SettingRow>
    </>
  );
}

function AppearancePane() {
  const [theme, setTheme] = useState<Theme>(savedTheme);
  return (
    <>
      <PaneHead title="Appearance" sub="Syncora is designed dark, for long sessions next to an editing application." />
      <SettingRow
        title="Theme"
        description="Light swaps the interface colours; the timeline keeps its dark canvas so waveforms read the same."
      >
        <Segmented
          label="Theme"
          value={theme}
          onChange={(t) => {
            setTheme(t);
            applyTheme(t);
            try {
              localStorage.setItem(THEME_KEY, t);
            } catch {
              // this session only
            }
          }}
          options={[
            { value: "dark", label: "Dark" },
            { value: "light", label: "Light" },
            { value: "system", label: "System" },
          ]}
        />
      </SettingRow>
    </>
  );
}

function MediaPane() {
  return (
    <>
      <PaneHead title="Media" sub="Syncora reads media where it is and never modifies, moves or deletes it." />
      <SettingRow
        title="Formats"
        description="Everything FFmpeg reads: MP4, MOV, MXF, MTS/M2TS, AVI, MKV, WAV, BWF, MP3, AAC, FLAC and more, from cameras, phones, drones and recorders."
      >
        <span className="sy-dim">All included</span>
      </SettingRow>
      <SettingRow
        title="RAW video"
        description="RED (.R3D), Blackmagic RAW (.braw), Canon RAW (.crm), ARRIRAW and Nikon N-RAW need their maker's SDK. Syncora lists them with a note: sync their proxies or audio instead."
      >
        <span className="sy-dim">Listed, not decoded</span>
      </SettingRow>
      <SettingRow
        title="Sidecar files"
        description="Low-resolution previews (.LRF, .THM), peak files, LUTs and camera metadata are recognised and left out, so they never appear as failed media."
      >
        <span className="sy-dim">Left out</span>
      </SettingRow>
      <SettingRow
        title="Duplicates"
        description="The same file imported twice is left out; clips that only look alike (same name, time and length) are kept until you decide. Review them from the Media screen."
      >
        <Button
          variant="secondary"
          size="compact"
          onClick={() => {
            useProd.getState().closeSettings();
            useProd.getState().openDialog("duplicates");
          }}
          disabled={!useApp.getState().project}
        >
          Review duplicates
        </Button>
      </SettingRow>
    </>
  );
}

function ProxyPane() {
  return (
    <>
      <PaneHead
        title="Proxy"
        sub="Syncora never needs proxies: it analyses the audio of the original files and previews them directly."
      />
      <SettingRow
        title="Analysis"
        description="Audio is decoded once into a small analysis cache (see Storage). Original video is not transcoded."
      >
        <span className="sy-dim">From originals</span>
      </SettingRow>
      <SettingRow
        title="Exported timelines"
        description="Exports link to the original files, so your editing application can make its own proxies."
      >
        <span className="sy-dim">Originals</span>
      </SettingRow>
    </>
  );
}

function ExportPane() {
  const [defaults, setDefaults] = useState(loadExportDefaults);
  const change = (patch: Partial<typeof defaults>) => {
    const next = { ...defaults, ...patch };
    setDefaults(next);
    saveExportDefaults(next);
  };
  return (
    <>
      <PaneHead title="Export" sub="Defaults for Export XML (⌘E). Each export can still change them." />
      <SettingRow
        title="Format"
        description="FCP 7 XML for Premiere Pro and DaVinci Resolve; FCPXML for Final Cut Pro and Resolve."
      >
        <Segmented
          label="Default format"
          value={defaults.format}
          onChange={(format) => change({ format })}
          options={[
            { value: "xmeml", label: "FCP 7 XML" },
            { value: "fcpxml", label: "FCPXML" },
          ]}
        />
      </SettingRow>
      <SettingRow title="Start timecode" description="Where the exported sequence starts.">
        <input
          className="sy-input sy-settings__tc"
          value={defaults.startTimecode}
          aria-label="Start timecode"
          onChange={(e) => change({ startTimecode: e.target.value })}
        />
      </SettingRow>
      <SettingRow
        title="Include clips to review"
        description="Clips below the review threshold are exported where Syncora placed them (on their own tracks); off leaves them out."
      >
        <Toggle
          label="Include clips to review"
          on={defaults.includeUncertain}
          onChange={(on) => change({ includeUncertain: on })}
        />
      </SettingRow>
    </>
  );
}

const SHORTCUTS: [string, string][] = [
  ["⌘1 … ⌘5", "Stages: Media, Sync, Analyze, Timeline, Export"],
  ["⇧S", "Sync all"],
  ["⌘.", "Pause or resume processing"],
  ["⌘B", "Run in background (back to Media)"],
  ["⌘K", "Search moments"],
  ["⇧A", "AI sync for the selected clip"],
  ["⌘↵", "Accept the AI sync proposal"],
  ["⌥→", "Next AI sync candidate"],
  ["Space", "Play or pause (transcript) · preview side by side (AI sync)"],
  ["J K L", "Back 5 s · pause · play (transcript)"],
  ["⇧↑ ⇧↓", "Previous / next transcript segment"],
  ["M", "Add a marker at the playhead (transcript)"],
  ["F2", "Rename the focused speaker"],
  ["← →", "Nudge the selected clip by a frame"],
  ["⌥← ⌥→", "Nudge the selected clip by a millisecond"],
  ["⌘Z ⇧⌘Z", "Undo / redo a correction"],
  ["⌘= ⌘- ⌘0", "Zoom in, out, to fit"],
  ["⌘E", "Export XML"],
  ["⌘I ⇧⌘I", "Import files / a folder"],
  ["⌘,", "Settings"],
];

function ShortcutsPane() {
  return (
    <>
      <PaneHead title="Keyboard shortcuts" sub="On Windows and Linux, ⌘ is Ctrl, ⌥ is Alt and ⇧ is Shift." />
      <div className="sy-kv sy-settings__facts sy-settings__shortcuts">
        {SHORTCUTS.map(([keys, what]) => (
          <div key={keys} className="sy-settings__shortcut">
            <dt className="sy-mono">{shortcut(keys)}</dt>
            <dd>{what}</dd>
          </div>
        ))}
      </div>
    </>
  );
}

function PrivacyPane() {
  return (
    <>
      <PaneHead title="Privacy" sub="Your media, transcripts and projects stay on this computer." />
      <SettingRow
        title="Analysis and AI"
        description="Audio analysis, transcription, speaker recognition, search and AI sync run locally with models shipped with Syncora."
      >
        <span className="sy-dim">On this computer</span>
      </SettingRow>
      <SettingRow
        title="Network"
        description="Syncora connects to the internet only when you download an additional speech model (from the sherpa-onnx releases on GitHub)."
      >
        <span className="sy-dim">Model downloads only</span>
      </SettingRow>
      <SettingRow title="Usage data" description="Syncora collects no analytics, telemetry or crash reports.">
        <span className="sy-dim">None</span>
      </SettingRow>
    </>
  );
}

function UpdatesPane() {
  const hello = useApp((s) => s.engine.hello);
  return (
    <>
      <PaneHead
        title="Updates"
        sub="New versions are published on the Syncora releases page, with installers for Windows and macOS."
      />
      <div className="sy-kv sy-settings__facts">
        <dt>This version</dt>
        <dd>{hello?.version ?? "…"}</dd>
        <dt>Releases</dt>
        <dd className="sy-mono">github.com/ZABRONMGAYA/ZABRONMGAYA/releases</dd>
      </div>
      <SettingRow
        title="Installing an update"
        description="Run the new installer over this one: projects, settings, downloaded models and the analysis cache are kept."
      >
        <span className="sy-dim">Manual</span>
      </SettingRow>
    </>
  );
}
