// Production-scale state: the media index (every clip, compact), pipeline progress, results, bins, search and
// the multi-selection. Kept apart from the timeline store so a 4,000-clip index never re-renders the timeline.
import { create } from "zustand";

import { call } from "../api/client";
import type {
  Device,
  MediaRow,
  OfflineVolumes,
  PipelineStatus,
  Priority,
  ProjectSummary,
  Session,
  SyncSummary,
} from "../api/contract";
import { matches, parseQuery, rowsFromIndex } from "../lib/query";
import { useApp } from "./store";

export type Stage = "media" | "sync" | "analyze" | "timeline" | "export";
export type SettingsCategory = "general" | "performance" | "synchronization" | "storage" | "about";
export type Bin =
  | "all"
  | "review"
  | "unmatched"
  | "offline"
  | "duplicates"
  | "failed"
  | `device:${number}`
  | `session:${number}`;

export interface ProdState {
  stage: Stage;
  syncView: "analysis" | "results";
  mediaView: "grid" | "list";
  importing: boolean;
  settings: SettingsCategory | null;
  pipeline: PipelineStatus | null;
  rows: MediaRow[];
  devices: (Device & { clips: number })[];
  sessions: Session[];
  indexVersion: number;
  summary: SyncSummary | null;
  bin: Bin;
  query: string;
  selection: Set<number>;
  anchor: number | null;
  resume: ProjectSummary["resume"];
  offline: OfflineVolumes | null;
  dialog: "duplicates" | "errors" | "resume" | "assign-camera" | "assign-session" | "remove" | null;

  reset(project: ProjectSummary | null): void;
  setStage(stage: Stage): void;
  setSyncView(view: "analysis" | "results"): void;
  setMediaView(view: "grid" | "list"): void;
  setImporting(on: boolean): void;
  openSettings(category?: SettingsCategory): void;
  closeSettings(): void;
  setBin(bin: Bin): void;
  setQuery(query: string): void;
  openDialog(dialog: ProdState["dialog"]): void;

  loadIndex(): Promise<void>;
  scheduleIndex(): void;
  loadSummary(): Promise<void>;
  loadOffline(): Promise<void>;
  onPipeline(status: PipelineStatus): void;

  select(id: number, mode: "replace" | "toggle" | "range", ordered: number[]): void;
  selectMany(ids: number[]): void;
  clearSelection(): void;

  addMedia(paths: string[]): Promise<void>;
  pause(): Promise<void>;
  resumePipeline(): Promise<void>;
  restart(): Promise<void>;
  cancelAll(): Promise<void>;
  cancelClips(ids: number[]): Promise<void>;
  retry(ids?: number[]): Promise<void>;
  prioritize(ids: number[], priority?: Priority): Promise<void>;
  analyze(ids: number[]): Promise<void>;
  remove(ids: number[]): Promise<void>;
  assignCamera(ids: number[], target: number | { name: string }): Promise<void>;
  assignSession(ids: number[], target: number | { label: string } | null): Promise<void>;
  startSync(ids?: number[]): Promise<void>;
  cancelSync(): Promise<void>;
}

let indexTimer: ReturnType<typeof setTimeout> | null = null;
let lastIndex = 0;
const INDEX_INTERVAL_MS = 1500;

function run<T>(action: () => Promise<T>): Promise<T | undefined> {
  return useApp.getState().run(action);
}

export const useProd = create<ProdState>((set, get) => ({
  stage: "media",
  syncView: "analysis",
  mediaView: "grid",
  importing: false,
  settings: null,
  pipeline: null,
  rows: [],
  devices: [],
  sessions: [],
  indexVersion: -1,
  summary: null,
  bin: "all",
  query: "",
  selection: new Set(),
  anchor: null,
  resume: null,
  offline: null,
  dialog: null,

  reset(project) {
    set({
      stage: project && project.clips === 0 ? "media" : "media",
      syncView: project?.last_run ? "results" : "analysis",
      importing: project !== null && project.clips === 0,
      pipeline: null,
      rows: [],
      devices: [],
      sessions: [],
      indexVersion: -1,
      summary: null,
      bin: "all",
      query: "",
      selection: new Set(),
      anchor: null,
      resume: project?.resume ?? null,
      offline: null,
      dialog: project?.resume ? "resume" : null,
    });
    if (project) {
      void get().loadIndex();
      void get().loadSummary();
      void get().loadOffline();
      void call("pipeline.status", {}).then(
        (p) => set({ pipeline: p }),
        () => undefined,
      );
    }
  },

  setStage(stage) {
    set({ stage });
    if (stage === "sync") void get().loadSummary();
  },
  setSyncView(syncView) {
    set({ syncView });
  },
  setMediaView(mediaView) {
    set({ mediaView });
  },
  setImporting(importing) {
    set({ importing });
  },
  openSettings(category = "synchronization") {
    set({ settings: category });
  },
  closeSettings() {
    set({ settings: null });
  },
  setBin(bin) {
    set({ bin });
  },
  setQuery(query) {
    set({ query });
  },
  openDialog(dialog) {
    set({ dialog });
  },

  async loadIndex() {
    if (!useApp.getState().project) return;
    lastIndex = Date.now();
    try {
      const index = await call("media.index", {});
      const rows = rowsFromIndex(index.columns, index.rows);
      const ids = new Set(rows.map((r) => r.clip_id));
      set((s) => ({
        rows,
        devices: index.devices,
        sessions: index.sessions,
        indexVersion: index.version,
        selection: new Set([...s.selection].filter((id) => ids.has(id))),
      }));
    } catch {
      // project closed meanwhile
    }
  },

  scheduleIndex() {
    if (indexTimer) return;
    const wait = Math.max(0, INDEX_INTERVAL_MS - (Date.now() - lastIndex));
    indexTimer = setTimeout(() => {
      indexTimer = null;
      void get().loadIndex();
    }, wait);
  },

  async loadSummary() {
    if (!useApp.getState().project) return;
    try {
      set({ summary: await call("sync.summary", {}) });
    } catch {
      // no project
    }
  },

  async loadOffline() {
    if (!useApp.getState().project) return;
    try {
      set({ offline: await call("media.offline", {}) });
    } catch {
      // no project
    }
  },

  onPipeline(status) {
    const before = get().pipeline;
    set({ pipeline: status });
    const done = (p: PipelineStatus | null) =>
      p ? Object.values(p.stages).reduce((n, s) => n + s.done + s.failed + s.skipped, 0) : 0;
    if (done(status) !== done(before) || status.discovery.total !== before?.discovery.total) get().scheduleIndex();
  },

  select(id, mode, ordered) {
    set((s) => {
      if (mode === "toggle") {
        const next = new Set(s.selection);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return { selection: next, anchor: id };
      }
      if (mode === "range" && s.anchor !== null) {
        const a = ordered.indexOf(s.anchor);
        const b = ordered.indexOf(id);
        if (a >= 0 && b >= 0) {
          const [lo, hi] = a < b ? [a, b] : [b, a];
          return { selection: new Set([...s.selection, ...ordered.slice(lo, hi + 1)]) };
        }
      }
      return { selection: new Set([id]), anchor: id };
    });
  },
  selectMany(ids) {
    set({ selection: new Set(ids), anchor: ids[0] ?? null });
  },
  clearSelection() {
    set({ selection: new Set(), anchor: null });
  },

  async addMedia(paths) {
    await run(async () => {
      await call("media.add", { paths });
      set({ importing: true, stage: "media" });
      useApp
        .getState()
        .toast("info", `Looking for media in ${paths.length === 1 ? "1 location" : `${paths.length} locations`}…`);
    });
  },

  async pause() {
    await run(async () => set({ pipeline: await call("pipeline.pause", {}) }));
  },
  async resumePipeline() {
    await run(async () => set({ pipeline: await call("pipeline.resume", {}), resume: null, dialog: null }));
  },
  async restart() {
    await run(async () => set({ pipeline: await call("pipeline.restart", {}), resume: null, dialog: null }));
  },
  async cancelAll() {
    await run(async () => {
      const { cancelled } = await call("tasks.cancel", {});
      await call("sync.cancel", {});
      useApp.getState().toast("info", `Cancelled ${cancelled} pending task(s). Finished work is kept.`);
    });
  },
  async cancelClips(ids) {
    await run(async () => {
      const { cancelled } = await call("tasks.cancel", { clip_ids: ids });
      useApp.getState().toast("info", `Cancelled ${cancelled} task(s).`);
    });
  },
  async retry(ids) {
    await run(async () => {
      const { retried } = await call("tasks.retry", ids ? { clip_ids: ids, statuses: ["failed", "cancelled"] } : {});
      useApp.getState().toast("info", retried ? `Retrying ${retried} task(s).` : "Nothing to retry.");
    });
  },
  async prioritize(ids, priority = "high") {
    await run(async () => {
      const { changed } = await call("tasks.prioritize", { clip_ids: ids, priority });
      useApp.getState().toast("info", `Moved ${changed} task(s) to the front of the queue.`);
    });
  },
  async analyze(ids) {
    await run(async () => {
      const { queued } = await call("tasks.analyze", { clip_ids: ids, priority: "high" });
      useApp.getState().toast("info", `Queued ${queued} clip(s) for audio analysis.`);
    });
  },
  async remove(ids) {
    await run(async () => {
      const { removed } = await call("media.remove", { clip_ids: ids });
      set({ selection: new Set(), dialog: null });
      useApp.getState().toast("success", `Removed ${removed} clip(s) from the project. The files were not touched.`);
      await get().loadIndex();
      await useApp.getState().refresh();
    });
  },
  async assignCamera(ids, target) {
    await run(async () => {
      const device =
        typeof target === "number"
          ? target
          : (await call("device.create", { name: target.name, kind: "camera" })).device_id;
      await call("clip.assign_device", { clip_ids: ids, device_id: device });
      set({ dialog: null });
      await get().loadIndex();
    });
  },
  async assignSession(ids, target) {
    await run(async () => {
      if (target !== null && typeof target === "object")
        await call("session.create", { label: target.label, clip_ids: ids });
      else await call("session.assign", { clip_ids: ids, session_id: target });
      set({ dialog: null });
      await get().loadIndex();
    });
  },
  async startSync(ids) {
    await run(async () => {
      if (ids?.length) await call("tasks.prioritize", { clip_ids: ids, priority: "high" });
      await call("sync.start", {});
      set({ stage: "sync", syncView: "analysis" });
    });
  },
  async cancelSync() {
    await run(async () => {
      await call("sync.cancel", {});
      useApp.getState().toast("info", "Synchronisation cancelled. Verified matches are kept for the next run.");
    });
  },
}));

/** Rows in the current bin and search, in display order. */
export function visibleRows(state: Pick<ProdState, "rows" | "bin" | "query">): MediaRow[] {
  const clauses = parseQuery(state.query);
  const bin = state.bin;
  return state.rows.filter((r) => {
    if (bin !== "all") {
      if (bin.startsWith("device:") && r.device_id !== Number(bin.slice(7))) return false;
      if (bin.startsWith("session:") && r.session_id !== Number(bin.slice(8))) return false;
      if (bin === "review" && r.category !== "review") return false;
      if (bin === "unmatched" && !(r.session_id === null && r.category !== "pending")) return false;
      if (bin === "offline" && r.media_status === "online") return false;
      if (bin === "duplicates" && r.duplicate_of === null) return false;
      if (bin === "failed" && r.category !== "failed") return false;
    }
    return clauses.length === 0 || matches(r, clauses);
  });
}
