// S13 Settings: Synchronization (as designed), Performance (resources, workers), Storage (cache), About.
import { X } from "lucide-react";
import { useEffect, useState } from "react";

import { call } from "../../api/client";
import type { CacheInfo, SystemResources, WorkerPlan } from "../../api/contract";
import { Button, Segmented, SettingRow, Toggle } from "../../design-system/components";
import { type SettingsCategory, useProd } from "../../state/production";
import { useApp } from "../../state/store";
import { formatBytes } from "../media/labels";

const CATEGORIES: { id: SettingsCategory | null; label: string }[] = [
  { id: "general", label: "General" },
  { id: null, label: "Appearance" },
  { id: "performance", label: "Performance" },
  { id: null, label: "AI" },
  { id: null, label: "Transcription" },
  { id: "synchronization", label: "Synchronization" },
  { id: null, label: "Media" },
  { id: null, label: "Proxy" },
  { id: null, label: "Export" },
  { id: null, label: "Keyboard shortcuts" },
  { id: "storage", label: "Storage" },
  { id: null, label: "Privacy" },
  { id: null, label: "Updates" },
  { id: "about", label: "About" },
];

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
            disabled={c.id === null}
            title={c.id === null ? "Not in this version of Syncora" : undefined}
            onClick={() => c.id && useProd.getState().openSettings(c.id)}
            data-testid={c.id ? `settings-${c.id}` : undefined}
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
        description="Not included in this version: clips without an audio match go to an extended audio search, then to manual sync."
      >
        <Toggle label="AI fallback" on={false} disabled onChange={() => undefined} />
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
