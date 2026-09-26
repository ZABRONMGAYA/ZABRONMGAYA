// The engine's JSON-RPC contract (docs/ARCHITECTURE.md §6), as the desktop app sees it.
// Kept in step with engine/src/mcsync/service/app.py; the e2e tests exercise every method used here.

export const PROTOCOL_VERSION = 1;

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

export interface Hello {
  name: string;
  version: string;
  protocol: number;
  ffmpeg: string | null;
  cache_dir: string;
  workers: number;
}

export interface ProjectSettings {
  mode: SyncMode;
  reference_clip_id: number | null;
  timecode_jam_synced: boolean;
  use_creation_time: boolean;
}

export interface ProjectSummary {
  path: string;
  name: string;
  settings: ProjectSettings;
  clips: number;
  offline: number;
  devices: number;
  last_run: number | null;
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
  "media.import": [{ paths: string[]; recursive?: boolean }, JobRef];
  "media.list": [Record<string, never>, MediaList];
  "media.remove": [{ clip_ids: number[] }, MediaList];
  "device.update": [{ device_id: number; name?: string; kind?: DeviceKind }, MediaList];
  "clip.assign_device": [{ clip_id: number; device_id: number }, MediaList];
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
  "job.cancel": [{ job_id: string }, { cancelled: boolean }];
  "job.list": [Record<string, never>, JobSummary[]];
}

export type Method = keyof EngineMethods;
export type Params<M extends Method> = EngineMethods[M][0];
export type Result<M extends Method> = EngineMethods[M][1];
export const ENGINE_METHODS: readonly Method[] = [
  "engine.hello",
  "project.create",
  "project.open",
  "project.close",
  "project.info",
  "project.update_settings",
  "media.import",
  "media.list",
  "media.remove",
  "device.update",
  "clip.assign_device",
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
  | "zoom-fit";

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
  /** Filesystem path of a file dropped onto the window. */
  pathForFile(file: File): string;
  /** Bytes [offset, offset + length) of a waveform file inside the analysis cache. */
  readPeaks(directory: string, file: string, offset: number, length: number): Promise<Uint8Array>;
  platform: string;
}
