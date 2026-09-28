// The engine's JSON-RPC contract (docs/ARCHITECTURE.md §6), as the desktop app sees it.
// Kept in step with engine/src/mcsync/service/app.py; the e2e tests exercise every method used here.

export const PROTOCOL_VERSION = 2;

export type SyncMode = "hybrid" | "audio" | "timecode";
export type DeviceKind = "camera" | "recorder" | "phone" | "drone" | "other";
export type PlacementStatus = "synced" | "needs_review" | "unsynced";
export type CorrectionKind = "offset" | "clear_offset" | "reject_pair" | "unreject_pair" | "exclude" | "include";
export type ReviewReason =
  | "conflict"
  | "device_overlap"
  | "detached"
  | "uncertain"
  | "metadata_only"
  | "unsynced"
  | "offline";

export interface WorkerPlan {
  probe: number;
  analyze: number;
  match: number;
  reason: string;
}

export interface Hello {
  name: string;
  version: string;
  protocol: number;
  ffmpeg: string | null;
  cache_dir: string;
  workers: number;
  plan: WorkerPlan;
  worker_mode: "auto" | "manual";
}

export interface ProjectSettings {
  mode: SyncMode;
  reference_clip_id: number | null;
  timecode_jam_synced: boolean;
  use_creation_time: boolean;
  /** Below this confidence a placement is shown as REVIEW (default 0.85). */
  review_threshold: number;
}

export type SyncPhase = "waiting" | "planning" | "matching" | "extending" | "solving" | "done";

export interface SyncState {
  run_id: number;
  phase: SyncPhase;
  started: number;
  finished?: number;
  elapsed_s?: number;
  planned?: number;
  reused?: number;
  strategy?: "exhaustive" | "staged" | "timecode";
  queried?: number;
  to_query?: number;
  unmatched?: number;
  extended?: number;
  extended_planned?: boolean;
}

export interface ProjectSummary {
  path: string;
  name: string;
  settings: ProjectSettings;
  clips: number;
  offline: number;
  devices: number;
  last_run: number | null;
  /** Work left from the previous session (the app offers Resume / Restart). */
  resume: { pending: Record<string, number>; sync: SyncState | null } | null;
  paused: boolean;
}

export type TaskKind = "probe" | "analyze" | "match" | "extend";
export type TaskStatus = "pending" | "running" | "done" | "failed" | "skipped" | "cancelled";

export type StageCounts = Record<TaskStatus, number> & { rate_per_min: number | null };

export interface ActivityLine {
  time: string;
  what: string;
  text: string;
  level: "info" | "warn" | "error";
}

export interface PipelineStatus {
  state: "running" | "paused" | "idle";
  error: string | null;
  discovery: {
    total: number;
    by_kind: Record<string, number>;
    by_status: Record<string, number>;
    walking: boolean;
  };
  stages: Record<TaskKind, StageCounts>;
  sync: SyncState | null;
  aux: string | null;
  running: { kind: TaskKind; name: string; task_id: number; seconds: number }[];
  workers: { probe: number; analyze: number; match: number };
  activity: ActivityLine[];
}

export interface TaskRow {
  id: number;
  kind: TaskKind;
  target: string;
  clip_id: number | null;
  other_clip_id: number | null;
  stage: string | null;
  priority: number;
  status: TaskStatus;
  attempts: number;
  error: string | null;
  updated_at: string;
  name: string | null;
  other_name: string | null;
}

export type ResultCategory =
  | "synchronized"
  | "high_confidence"
  | "review"
  | "manual"
  | "failed"
  | "skipped"
  | "pending";

/** Column names of `media.index` rows, in order. */
export const INDEX_COLUMNS = [
  "clip_id",
  "name",
  "kind",
  "device_id",
  "device_name",
  "device_kind",
  "session_id",
  "duration_s",
  "fps",
  "width",
  "height",
  "codec",
  "sample_rate",
  "channels",
  "timecode",
  "creation_time",
  "size_bytes",
  "media_status",
  "duplicate_of",
  "duplicate_decision",
  "probe",
  "analysis",
  "sync_status",
  "confidence",
  "method",
  "start_s",
  "group",
  "category",
  "path",
] as const;

export interface MediaRow {
  clip_id: number;
  name: string;
  kind: "video" | "audio";
  device_id: number | null;
  device_name: string | null;
  device_kind: DeviceKind | null;
  session_id: number | null;
  duration_s: number | null;
  fps: string | null;
  width: number | null;
  height: number | null;
  codec: string | null;
  sample_rate: number | null;
  channels: number | null;
  timecode: string | null;
  creation_time: string | null;
  size_bytes: number;
  media_status: "online" | "offline" | "changed";
  duplicate_of: number | null;
  duplicate_decision: "keep" | "ignore" | null;
  /** identical: the same bytes (left out until kept); probable: same name, time and length (kept until ignored). */
  duplicate_reason: "identical" | "probable" | null;
  probe: TaskStatus | null;
  analysis: string | null;
  sync_status: PlacementStatus | null;
  confidence: number | null;
  method: string | null;
  start_s: number | null;
  group: number | null;
  category: ResultCategory;
  path: string;
}

export interface Session {
  id: number;
  label: string;
  start_at: string | null;
  end_at: string | null;
  group_no: number | null;
  source: "auto" | "manual";
  clips: number;
}

export interface MediaIndex {
  columns: string[];
  rows: unknown[][];
  devices: (Device & { clips: number })[];
  sessions: Session[];
  version: number;
}

export interface SourceSummary {
  device_id: number | null;
  name: string;
  kind: DeviceKind | null;
  clips: number;
  counts: Record<ResultCategory, number>;
  median_confidence: number | null;
  min_confidence: number | null;
}

export interface SyncSummary {
  clips: number;
  unreadable_files: number;
  counts: Record<ResultCategory, number>;
  sources: SourceSummary[];
  sessions: Session[];
  run: SyncState | null;
  last_run: number | null;
  threshold: number;
}

export interface Duplicate {
  media_id: number;
  path: string;
  filename: string;
  original_id: number;
  original_path: string;
  original_filename: string;
  decision: "keep" | "ignore" | null;
  reason: "identical" | "probable" | null;
  size_bytes: number;
  duration_s: number | null;
  clip_id: number;
}

export interface OfflineMedia {
  media_id: number;
  clip_id: number;
  path: string;
  filename: string;
  size_bytes: number;
  status: "offline" | "changed";
}

export interface OfflineVolumes {
  volumes: { volume: string; online: boolean; ignored: boolean; count: number; media: OfflineMedia[] }[];
}

export interface SystemResources {
  resources: {
    cpu_logical: number;
    cpu_usable: number;
    ram_total_bytes: number | null;
    ram_available_bytes: number | null;
    gpu: string | null;
    gpu_memory_bytes: number | null;
    platform: string;
  };
  recommended: WorkerPlan;
  plan: WorkerPlan;
  mode: "auto" | "manual";
  gpu_used: boolean;
  storage: {
    cache: { path: string; free_bytes: number; total_bytes: number };
    project: { path: string; free_bytes: number; total_bytes: number } | null;
  };
}

export interface CacheInfo {
  root: string;
  bytes: number;
  entries: number;
  project_bytes: number;
  disk: { path: string; free_bytes: number; total_bytes: number };
}

export interface AudioStream {
  index: number;
  channels: number;
  codec: string;
  sample_rate: number;
}

export interface ClipSummary {
  clip_id: number;
  name: string;
  path: string;
  status: "online" | "offline" | "changed";
  device_id: number | null;
  device_name: string | null;
  kind: DeviceKind | null;
  duration_s: number;
  frame_rate: string | null;
  vfr: boolean;
  timecode: string | null;
  creation_time: string | null;
  audio_streams: AudioStream[];
  audio_stream: number | null;
  audio_channel: number | null;
  chapter: { take: string; index: number } | null;
}

export interface Device {
  id: number;
  key: string;
  name: string;
  kind: DeviceKind;
  make: string | null;
  model: string | null;
  serial: string | null;
  color: string | null;
}

export interface MediaList {
  clips: ClipSummary[];
  devices: Device[];
}

export interface TimelineClip {
  clip_id: number;
  name: string;
  path: string;
  device_id: number | null;
  device_name: string;
  kind: DeviceKind;
  duration_s: number;
  has_video: boolean;
  has_audio: boolean;
  frame_rate: string | null;
  timecode: string | null;
  media_status: string;
  start_s: number | null;
  group: number | null;
  track: number | null;
  status: PlacementStatus;
  method: "reference" | "audio" | "timecode" | "metadata" | "chapter" | "manual" | "none";
  confidence: number;
  flags: string[];
  drift_ppm: number;
}

export interface TimelineTrack {
  index: number;
  device_id: number | null;
  device_name: string;
  kind: DeviceKind;
  lane: number;
}

export interface TimelineGroup {
  group: number;
  duration_s: number;
  origin_offset_s: number;
  tracks: TimelineTrack[];
  clips: TimelineClip[];
}

export interface ReviewItem {
  clip_id: number;
  reason: ReviewReason;
  flags: string[];
}

export interface Timeline {
  groups: TimelineGroup[];
  unsynced: TimelineClip[];
  review: ReviewItem[];
  reference_clip_id: number | null;
  stats: { clips: number; synced: number; needs_review: number; unsynced: number };
}

export interface SyncRunResult {
  run_id: number;
  pairs: number;
  reused: number;
  matched: number;
  timeline: Timeline;
}

export interface ImportResult {
  clip_ids: number[];
  problems: { path: string; message: string }[];
}

export interface Alternative {
  offset_s: number;
  coarse_psr: number;
  n_inliers: number;
  inlier_fraction: number;
}

export interface SnapResult {
  offset_s: number | null;
  confidence: number;
  status: "confident" | "uncertain" | "no_match";
  flags: string[];
  alternatives: Alternative[];
}

export interface WaveformInfo {
  directory: string;
  files: Record<string, string>;
  encoding: string;
  rate: number;
  samples: number;
  level_dbfs: number | null;
  audio_start_s: number;
}

export interface ClipMatch {
  other_clip_id: number;
  other_name: string;
  /** start(this clip) - start(other clip), seconds. */
  offset_s: number | null;
  confidence: number;
  status: "confident" | "uncertain" | "no_match";
  flags: string[];
  drift_ppm: number;
  rejected: boolean;
}

export type ExportFormat = "xmeml" | "fcpxml";

export interface ExportOptions {
  format: ExportFormat;
  path: string;
  /** "25", "24000/1001"…; omitted: the most common video rate. */
  sequence_rate?: string;
  start_timecode?: string;
  group?: number;
  include_uncertain?: boolean;
  name?: string;
}

export interface ExportReport {
  path: string;
  format: ExportFormat;
  sequence: {
    name: string;
    rate: string;
    width: number;
    height: number;
    start_timecode: string;
    duration_s: number;
    video_tracks: number;
    audio_tracks: number;
  };
  clips: {
    clip_id: number;
    name: string;
    track: string;
    start_s: number;
    placed_s: number;
    error_ms: number;
    status: PlacementStatus;
  }[];
  /** Largest placement error as the format's readers see it (xmeml: in points rounded to frames). */
  max_error_ms: number;
  /** xmeml only: in Premiere Pro, which reads the sub-frame in points. */
  max_error_ms_premiere?: number;
  skipped: { clip_id: number; name: string; reason: string }[];
  warnings: string[];
}

export interface JobRef {
  job_id: string;
}

export interface JobSummary {
  job_id: string;
  kind: string;
  status: "running" | "done" | "failed" | "cancelled";
  progress: number;
  message: string;
  error: string | null;
}

export interface Correction {
  id: number;
  kind: CorrectionKind;
  clip_id: number;
  other_clip_id: number | null;
  offset_s: number | null;
  created_at: string;
  undone_at: string | null;
}

/** method → [params, result] */
export interface EngineMethods {
  "engine.hello": [{ client?: string }, Hello];
  "project.create": [{ path: string; name?: string }, ProjectSummary];
  "project.open": [{ path: string }, ProjectSummary];
  "project.close": [Record<string, never>, { ok: boolean }];
  "project.info": [Record<string, never>, ProjectSummary];
  "project.update_settings": [Partial<ProjectSettings>, ProjectSettings];
  "engine.configure": [
    { workers?: "auto" | Partial<Omit<WorkerPlan, "reason">> },
    { plan: WorkerPlan; mode: "auto" | "manual"; recommended: WorkerPlan },
  ];
  "system.resources": [Record<string, never>, SystemResources];
  "system.disk_speed": [{ path?: string }, { path: string; write_bytes_per_s: number }];
  "media.import": [{ paths: string[]; recursive?: boolean }, JobRef];
  "media.add": [{ paths: string[]; recursive?: boolean; priority?: Priority }, { request_id: number }];
  "media.list": [Record<string, never>, MediaList];
  "media.index": [Record<string, never>, MediaIndex];
  "media.remove": [{ clip_ids: number[] }, { removed: number }];
  "media.rescan": [Record<string, never>, JobRef];
  "media.duplicates": [Record<string, never>, Duplicate[]];
  "media.decide_duplicates": [{ media_ids: number[]; decision: "keep" | "ignore" }, { duplicates: Duplicate[] }];
  "media.offline": [Record<string, never>, OfflineVolumes];
  "media.relink_folder": [{ folder: string }, OfflineVolumes & { relinked: number; not_matching: string[] }];
  "media.relink_file": [{ media_id: number; path: string; force?: boolean }, OfflineVolumes];
  "media.ignore_offline": [{ volume: string; ignore?: boolean }, OfflineVolumes];
  "media.thumbnails": [{ clip_ids: number[] }, { thumbnails: [number, string][] }];
  "device.update": [{ device_id: number; name?: string; kind?: DeviceKind }, { devices: Device[] }];
  "device.create": [{ name: string; kind?: DeviceKind }, { device_id: number }];
  "clip.assign_device": [{ clip_ids: number[]; device_id: number }, { assigned: number; devices: Device[] }];
  "session.list": [Record<string, never>, Session[]];
  "session.create": [{ label: string; clip_ids: number[] }, { session_id: number }];
  "session.assign": [{ clip_ids: number[]; session_id: number | null }, { sessions: Session[] }];
  "pipeline.status": [Record<string, never>, PipelineStatus];
  "pipeline.pause": [Record<string, never>, PipelineStatus];
  "pipeline.resume": [Record<string, never>, PipelineStatus];
  "pipeline.restart": [{ sync?: boolean }, PipelineStatus];
  "tasks.list": [{ statuses: TaskStatus[]; kinds?: TaskKind[]; limit?: number; offset?: number }, TaskRow[]];
  "tasks.cancel": [
    { ids?: number[]; clip_ids?: number[]; kinds?: TaskKind[]; running?: boolean },
    { cancelled: number },
  ];
  "tasks.retry": [
    { statuses?: TaskStatus[]; ids?: number[]; clip_ids?: number[]; kinds?: TaskKind[] },
    { retried: number },
  ];
  "tasks.prioritize": [{ clip_ids: number[]; priority?: Priority }, { changed: number }];
  "tasks.analyze": [{ clip_ids?: number[]; priority?: Priority }, { queued: number }];
  "sync.start": [{ mode?: SyncMode; reference_clip_id?: number; timecode_jam_synced?: boolean }, { run_id: number }];
  "sync.cancel": [Record<string, never>, { cancelled: boolean }];
  "sync.summary": [Record<string, never>, SyncSummary];
  "cache.info": [Record<string, never>, CacheInfo];
  "cache.clear_unused": [Record<string, never>, CacheInfo & { freed_bytes: number }];
  "project.stats": [
    Record<string, never>,
    { bytes: number; wal_bytes: number; rows: Record<string, number>; path: string },
  ];
  "clip.set_audio": [{ clip_id: number; stream_index: number | null; channel?: number | null }, ClipSummary];
  "sync.run": [{ mode?: SyncMode; reference_clip_id?: number; timecode_jam_synced?: boolean }, JobRef];
  "sync.solve": [Record<string, never>, Timeline];
  "sync.snap": [{ clip_id: number; anchor_clip_id: number; approx_offset_s: number; radius_s?: number }, SnapResult];
  "sync.matches": [{ clip_id: number }, ClipMatch[]];
  "correction.add": [{ kind: CorrectionKind; clip_id: number; other_clip_id?: number; offset_s?: number }, Timeline];
  "correction.undo": [Record<string, never>, Timeline];
  "correction.redo": [Record<string, never>, Timeline];
  "correction.list": [Record<string, never>, Correction[]];
  "timeline.get": [Record<string, never>, Timeline];
  "waveform.info": [{ clip_id: number }, WaveformInfo];
  "export.xml": [ExportOptions, ExportReport];
  "job.cancel": [{ job_id: string }, { cancelled: boolean }];
  "job.list": [Record<string, never>, JobSummary[]];
}

export type Method = keyof EngineMethods;
export type Params<M extends Method> = EngineMethods[M][0];
export type Result<M extends Method> = EngineMethods[M][1];
export type Priority = "high" | "normal" | "low";

export const ENGINE_METHODS: readonly Method[] = [
  "engine.hello",
  "project.create",
  "project.open",
  "project.close",
  "project.info",
  "project.update_settings",
  "engine.configure",
  "system.resources",
  "system.disk_speed",
  "media.import",
  "media.add",
  "media.list",
  "media.index",
  "media.remove",
  "media.rescan",
  "media.duplicates",
  "media.decide_duplicates",
  "media.offline",
  "media.relink_folder",
  "media.relink_file",
  "media.ignore_offline",
  "media.thumbnails",
  "device.update",
  "device.create",
  "clip.assign_device",
  "session.list",
  "session.create",
  "session.assign",
  "pipeline.status",
  "pipeline.pause",
  "pipeline.resume",
  "pipeline.restart",
  "tasks.list",
  "tasks.cancel",
  "tasks.retry",
  "tasks.prioritize",
  "tasks.analyze",
  "sync.start",
  "sync.cancel",
  "sync.summary",
  "cache.info",
  "cache.clear_unused",
  "project.stats",
  "clip.set_audio",
  "sync.run",
  "sync.solve",
  "sync.snap",
  "sync.matches",
  "correction.add",
  "correction.undo",
  "correction.redo",
  "correction.list",
  "timeline.get",
  "waveform.info",
  "export.xml",
  "job.cancel",
  "job.list",
];

export type EngineState = "starting" | "ready" | "crashed" | "stopped";

export interface EngineStatus {
  state: EngineState;
  hello: Hello | null;
  error: string | null;
}

export type EngineEvent =
  | { method: "job.progress"; params: { job_id: string; kind: string; progress: number; message: string } }
  | { method: "job.done"; params: { job_id: string; kind: string; result: unknown } }
  | { method: "job.failed"; params: { job_id: string; kind: string; cancelled: boolean; error: string } }
  | { method: "media.imported"; params: { clip_id: number; path: string } }
  | { method: "media.analyzed"; params: { clip_ids: number[] } }
  | { method: "media.thumbnails"; params: { thumbnails: [number, string][] } }
  | { method: "pipeline.progress"; params: PipelineStatus }
  | { method: "pipeline.matches"; params: { run_id: number; count: number } }
  | { method: "pipeline.error"; params: { error: string } }
  | {
      method: "pipeline.sync_finished";
      params: { run_id: number; status: "completed" | "cancelled" | "failed"; error?: string; timeline?: Timeline };
    }
  | { method: "engine.status"; params: EngineStatus }
  | { method: "menu"; params: { command: MenuCommand } };

export type MenuCommand =
  | "new-project"
  | "open-project"
  | "close-project"
  | "import-files"
  | "import-folder"
  | "sync"
  | "undo"
  | "redo"
  | "zoom-in"
  | "zoom-out"
  | "zoom-fit"
  | "export"
  | "settings";

export interface RpcFailure {
  code: number;
  message: string;
}

export type InvokeResponse<T> = { ok: true; result: T } | { ok: false; error: RpcFailure };

/** What the preload script exposes as `window.mcsync`. */
export interface Bridge {
  /** Resolves with an envelope; `api/client.ts` turns failures into EngineError. */
  invoke<M extends Method>(method: M, params: Params<M>): Promise<InvokeResponse<Result<M>>>;
  onEvent(listener: (event: EngineEvent) => void): () => void;
  engineStatus(): Promise<EngineStatus>;
  chooseMedia(kind: "files" | "folder"): Promise<string[]>;
  chooseProjectToOpen(): Promise<string | null>;
  chooseProjectToCreate(defaultName: string): Promise<string | null>;
  /** A folder to look for offline media in; null if cancelled. */
  chooseFolder(title: string): Promise<string | null>;
  /** One replacement file for offline media; null if cancelled. */
  chooseFile(title: string): Promise<string | null>;
  /** Save a text report (results summary); returns where, null if cancelled. */
  saveReport(defaultName: string, contents: string): Promise<string | null>;
  /** Where to write an export; null if cancelled. */
  chooseExportPath(defaultName: string, format: ExportFormat): Promise<string | null>;
  /** Reveal a file this app exported in the system file manager. */
  showInFolder(path: string): Promise<void>;
  /** Filesystem path of a file dropped onto the window. */
  pathForFile(file: File): string;
  /** A poster frame (PNG) from the engine's cache. */
  readThumbnail(path: string): Promise<Uint8Array>;
  /** Bytes [offset, offset + length) of a waveform file inside the analysis cache. */
  readPeaks(directory: string, file: string, offset: number, length: number): Promise<Uint8Array>;
  platform: string;
}
