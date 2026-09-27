// Application state: one store, driven by user actions and engine notifications.
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";

import { EngineError, ErrorCode, bridge, call } from "../api/client";
import type {
  ClipMatch,
  CorrectionKind,
  EngineEvent,
  EngineStatus,
  ExportOptions,
  ExportReport,
  ImportResult,
  MediaList,
  MenuCommand,
  ProjectSettings,
  ProjectSummary,
  SnapResult,
  SyncRunResult,
  Timeline,
  TimelineClip,
  TimelineGroup,
} from "../api/contract";
import { formatOffset } from "../lib/format";
import { type View, anchorClip, clampView, fitView, offsetForStart, zoomAround } from "../features/timeline/geometry";
import { forgetPeaks } from "../features/timeline/peaks";
import { useProd } from "./production";
import { useThumbs } from "./thumbs";

export interface JobState {
  id: string;
  kind: string;
  progress: number;
  message: string;
  status: "running" | "done" | "failed" | "cancelled";
}

export interface Toast {
  id: number;
  kind: "info" | "success" | "error";
  text: string;
}

const RECENT_KEY = "syncora.recentProjects";
const LEGACY_RECENT_KEY = "mcsync.recentProjects";

function loadRecent(): string[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) ?? localStorage.getItem(LEGACY_RECENT_KEY) ?? "[]") as string[];
  } catch {
    return [];
  }
}

function saveRecent(paths: string[]): void {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(paths.slice(0, 8)));
  } catch {
    // storage unavailable: recent projects are a convenience only
  }
}

export interface AppState {
  engine: EngineStatus;
  project: ProjectSummary | null;
  media: MediaList;
  timeline: Timeline | null;
  lastSync: { pairs: number; reused: number; matched: number } | null;
  jobs: Record<string, JobState>;
  selected: number | null;
  group: number;
  view: View;
  timelineWidth: number;
  /** Timeline time of the edit cursor (set by clicking the timeline), or null. */
  cursorS: number | null;
  /** Bumped whenever analysis results change, so waveforms reload. */
  peaksEpoch: number;
  snaps: Record<number, SnapResult>;
  matches: Record<number, ClipMatch[]>;
  toasts: Toast[];
  recent: string[];
  exportOpen: boolean;
  lastExport: ExportReport | null;

  init(): () => void;
  toast(kind: Toast["kind"], text: string): void;
  dismiss(id: number): void;
  run<T>(action: () => Promise<T>): Promise<T | undefined>;

  newProject(): Promise<void>;
  openProject(path?: string): Promise<void>;
  closeProject(): Promise<void>;
  refresh(): Promise<void>;
  updateSettings(settings: Partial<ProjectSettings>): Promise<void>;
  importMedia(kind: "files" | "folder"): Promise<void>;
  importPaths(paths: string[]): Promise<void>;
  sync(): Promise<void>;
  cancelJob(id: string): Promise<void>;

  select(clipId: number | null): void;
  setGroup(group: number): void;
  correct(kind: CorrectionKind, clipId: number, otherClipId?: number, offsetS?: number): Promise<void>;
  moveClip(clipId: number, startS: number): Promise<void>;
  confirm(clipId: number): Promise<void>;
  placeAtCursor(clipId: number): Promise<void>;
  nudge(clipId: number, deltaS: number): Promise<void>;
  snap(clipId: number): Promise<void>;
  loadMatches(clipId: number): Promise<void>;
  undo(): Promise<void>;
  redo(): Promise<void>;
  setReference(clipId: number | null): Promise<void>;

  openExport(): void;
  closeExport(): void;
  /** Ask where to save, write the XML, keep the report. */
  exportXml(options: Omit<ExportOptions, "path">): Promise<ExportReport | undefined>;

  setView(view: View): void;
  setTimelineWidth(width: number): void;
  setCursor(timeS: number | null): void;
  invalidatePeaks(): void;
  zoom(factor: number, anchorS?: number): void;
  fit(): void;
  reveal(clipId: number): void;
}

let toastId = 0;
const nudgeTargets = new Map<number, number>();

export function findClip(timeline: Timeline | null, clipId: number | null): TimelineClip | undefined {
  if (!timeline || clipId === null) return undefined;
  for (const g of timeline.groups) for (const c of g.clips) if (c.clip_id === clipId) return c;
  return timeline.unsynced.find((c) => c.clip_id === clipId);
}

/**
 * Subscribe to some fields of the store: the component re-renders only when one of them changes
 * (not on every pan or zoom frame).
 */
export function usePick<K extends keyof AppState>(...keys: K[]): Pick<AppState, K> {
  return useApp(useShallow((s) => Object.fromEntries(keys.map((k) => [k, s[k]])) as Pick<AppState, K>));
}

/** The group the timeline shows (the main group if the selected one no longer exists). */
export function displayedGroup(state: Pick<AppState, "timeline" | "group">): TimelineGroup | undefined {
  const groups = state.timeline?.groups ?? [];
  return groups.find((g) => g.group === state.group) ?? groups[0];
}

export const useApp = create<AppState>((set, get) => ({
  engine: { state: "starting", hello: null, error: null },
  project: null,
  media: { clips: [], devices: [] },
  timeline: null,
  lastSync: null,
  jobs: {},
  selected: null,
  group: 0,
  view: { pxPerSec: 2, startS: -5 },
  timelineWidth: 1000,
  cursorS: null,
  peaksEpoch: 0,
  snaps: {},
  matches: {},
  toasts: [],
  recent: loadRecent(),
  exportOpen: false,
  lastExport: null,

  init() {
    const off = bridge().onEvent((event) => handleEvent(event));
    void bridge()
      .engineStatus()
      .then((engine) => set({ engine }));
    return off;
  },

  toast(kind, text) {
    const id = ++toastId;
    set((s) => ({ toasts: [...s.toasts, { id, kind, text }].slice(-5) }));
    if (kind !== "error") setTimeout(() => get().dismiss(id), 6000);
  },

  dismiss(id) {
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
  },

  async run(action) {
    try {
      return await action();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (err instanceof EngineError && err.code === ErrorCode.busy) get().toast("info", message);
      else get().toast("error", message);
      return undefined;
    }
  },

  // ------------------------------------------------------------ project

  async newProject() {
    const path = await bridge().chooseProjectToCreate("Untitled project");
    if (!path) return;
    await get().run(async () => {
      const project = await call("project.create", { path });
      afterOpen(project);
    });
  },

  async openProject(path) {
    const target = path ?? (await bridge().chooseProjectToOpen());
    if (!target) return;
    await get().run(async () => {
      const project = await call("project.open", { path: target });
      afterOpen(project);
      await get().refresh();
      get().fit();
      if (project.offline > 0) get().toast("error", `${project.offline} media file(s) are offline or changed.`);
    });
  },

  async closeProject() {
    await get().run(async () => {
      await call("project.close", {});
      set({ project: null, timeline: null, media: { clips: [], devices: [] }, selected: null, lastSync: null });
      useProd.getState().reset(null);
    });
  },

  async refresh() {
    if (!get().project) return;
    const [media, timeline, project] = await Promise.all([
      call("media.list", {}),
      call("timeline.get", {}),
      call("project.info", {}),
    ]);
    set({ media, timeline, project });
  },

  async updateSettings(settings) {
    await get().run(async () => {
      const updated = await call("project.update_settings", settings);
      set((s) => (s.project ? { project: { ...s.project, settings: updated } } : {}));
    });
  },

  // ------------------------------------------------------------- media

  async importMedia(kind) {
    const paths = await bridge().chooseMedia(kind);
    if (paths.length) await get().importPaths(paths);
  },

  async importPaths(paths) {
    await useProd.getState().addMedia(paths);
  },

  // -------------------------------------------------------------- sync

  async sync() {
    if (!get().project) return;
    await useProd.getState().startSync();
  },

  async cancelJob(id) {
    await get().run(() => call("job.cancel", { job_id: id }));
  },

  // -------------------------------------------------------- corrections

  select(clipId) {
    set({ selected: clipId });
    if (clipId !== null) void get().loadMatches(clipId);
  },

  setGroup(group) {
    set({ group });
    get().fit();
  },

  async correct(kind, clipId, otherClipId, offsetS) {
    await get().run(async () => {
      const timeline = await call("correction.add", {
        kind,
        clip_id: clipId,
        ...(otherClipId !== undefined ? { other_clip_id: otherClipId } : {}),
        ...(offsetS !== undefined ? { offset_s: offsetS } : {}),
      });
      set({ timeline, matches: {} });
      if (get().selected !== null) void get().loadMatches(get().selected!);
    });
  },

  async moveClip(clipId, startS) {
    const { timeline } = get();
    const g = displayedGroup(get());
    const anchor = g && anchorClip(g, timeline!.reference_clip_id);
    if (!g || !anchor) return;
    if (anchor.clip_id === clipId) {
      get().toast("info", "The reference clip defines the timeline; move the other clips instead.");
      return;
    }
    await get().correct("offset", clipId, anchor.clip_id, offsetForStart(startS, anchor));
  },

  async confirm(clipId) {
    const clip = findClip(get().timeline, clipId);
    if (clip?.start_s === null || clip?.start_s === undefined) return;
    await get().moveClip(clipId, clip.start_s);
  },

  async placeAtCursor(clipId) {
    const { cursorS } = get();
    if (cursorS === null) {
      get().toast("info", "Click the timeline where the clip should start, then place it.");
      return;
    }
    if (!displayedGroup(get())) {
      get().toast("info", "Synchronise first: clips are placed relative to the reference recording.");
      return;
    }
    await get().moveClip(clipId, cursorS);
  },

  async nudge(clipId, deltaS) {
    const clip = findClip(get().timeline, clipId);
    if (clip?.start_s === null || clip?.start_s === undefined) return;
    // Rapid key presses must add up, even before the previous nudge's timeline has come back.
    const target = (nudgeTargets.get(clipId) ?? clip.start_s) + deltaS;
    nudgeTargets.set(clipId, target);
    try {
      await get().moveClip(clipId, target);
    } finally {
      if (nudgeTargets.get(clipId) === target) nudgeTargets.delete(clipId);
    }
  },

  async snap(clipId) {
    const { timeline } = get();
    const g = displayedGroup(get());
    const clip = findClip(timeline, clipId);
    const anchor = g && anchorClip(g, timeline!.reference_clip_id);
    if (!g || !anchor || clip?.start_s === null || clip?.start_s === undefined) return;
    await get().run(async () => {
      const approx = offsetForStart(clip.start_s!, anchor);
      const result = await call("sync.snap", {
        clip_id: clipId,
        anchor_clip_id: anchor.clip_id,
        approx_offset_s: approx,
        radius_s: 2,
      });
      set((s) => ({ snaps: { ...s.snaps, [clipId]: result } }));
      if (result.offset_s !== null && result.status === "confident") {
        await get().correct("offset", clipId, anchor.clip_id, result.offset_s);
        get().toast("success", `Snapped to the audio (${formatOffset(result.offset_s - approx)}).`);
      } else {
        get().toast("info", "No confident audio match within ±2 s of this position.");
      }
    });
  },

  async loadMatches(clipId) {
    try {
      const matches = await call("sync.matches", { clip_id: clipId });
      set((s) => ({ matches: { ...s.matches, [clipId]: matches } }));
    } catch {
      // no project or no run yet
    }
  },

  async undo() {
    await get().run(async () => set({ timeline: await call("correction.undo", {}) }));
  },

  async redo() {
    await get().run(async () => set({ timeline: await call("correction.redo", {}) }));
  },

  async setReference(clipId) {
    await get().updateSettings({ reference_clip_id: clipId });
    await get().run(async () => set({ timeline: await call("sync.solve", {}) }));
  },

  // ------------------------------------------------------------ export

  openExport() {
    if (!get().timeline?.groups.length) {
      get().toast("info", "Synchronise first: there is no timeline to export yet.");
      return;
    }
    set({ exportOpen: true, lastExport: null });
  },

  closeExport() {
    set({ exportOpen: false });
  },

  async exportXml(options) {
    const project = get().project;
    if (!project) return undefined;
    const path = await bridge().chooseExportPath(project.name, options.format);
    if (!path) return undefined;
    return get().run(async () => {
      const report = await call("export.xml", { ...options, path });
      set({ lastExport: report });
      get().toast("success", `Exported ${report.clips.length} clips.`);
      return report;
    });
  },

  // -------------------------------------------------------------- view

  setView(view) {
    set({ view: clamped(view) });
  },

  setTimelineWidth(width) {
    set({ timelineWidth: width });
  },

  setCursor(timeS) {
    set({ cursorS: timeS });
  },

  invalidatePeaks() {
    forgetPeaks();
    set((s) => ({ peaksEpoch: s.peaksEpoch + 1 }));
  },

  zoom(factor, anchorS) {
    const { view, timelineWidth } = get();
    const anchor = anchorS ?? view.startS + timelineWidth / 2 / view.pxPerSec;
    set({ view: clamped(zoomAround(view, factor, anchor)) });
  },

  fit() {
    const { timelineWidth } = get();
    const g = displayedGroup(get());
    set({ view: fitView(g?.duration_s ?? 600, timelineWidth) });
  },

  reveal(clipId) {
    const { timeline, view, timelineWidth } = get();
    const clip = findClip(timeline, clipId);
    if (!clip || clip.start_s === null || clip.group === null) return;
    const visible = timelineWidth / view.pxPerSec;
    const start = clip.start_s;
    const inView = start >= view.startS && start + Math.min(clip.duration_s, visible) <= view.startS + visible;
    set({ group: clip.group, ...(inView ? {} : { view: { ...view, startS: start - visible * 0.1 } }) });
  },
}));

function clamped(view: View): View {
  const { timelineWidth } = useApp.getState();
  const g = displayedGroup(useApp.getState());
  return g ? clampView(view, g.duration_s, timelineWidth) : view;
}

function afterOpen(project: ProjectSummary): void {
  const recent = [project.path, ...useApp.getState().recent.filter((p) => p !== project.path)];
  saveRecent(recent);
  useApp.setState({
    project,
    recent,
    timeline: null,
    selected: null,
    group: 0,
    lastSync: null,
    snaps: {},
    matches: {},
    cursorS: null,
    exportOpen: false,
    lastExport: null,
    media: { clips: [], devices: [] },
  });
  useApp.getState().invalidatePeaks();
  useThumbs.getState().clear();
  useProd.getState().reset(project);
}

let peaksTimer: ReturnType<typeof setTimeout> | null = null;

/** Waveforms reload at most every few seconds while thousands of clips are being analysed. */
function schedulePeaks(): void {
  if (peaksTimer) return;
  peaksTimer = setTimeout(() => {
    peaksTimer = null;
    useApp.getState().invalidatePeaks();
  }, 3000);
}

async function afterSync(result: { status: string; error?: string; timeline?: Timeline }): Promise<void> {
  const app = useApp.getState();
  const prod = useProd.getState();
  if (result.status === "completed") {
    if (result.timeline) useApp.setState({ timeline: result.timeline, matches: {} });
    app.invalidatePeaks();
    await Promise.all([prod.loadSummary(), prod.loadIndex(), app.refresh()]);
    const summary = useProd.getState().summary;
    if (summary) {
      const c = summary.counts;
      const review = c.review;
      app.toast(
        "success",
        `Synchronised ${c.synchronized} of ${summary.clips} clips${review ? `; ${review} to review` : ""}.`,
      );
    }
    useProd.setState({ syncView: "results" });
    app.fit();
  } else if (result.status === "failed") {
    app.toast("error", `Synchronisation failed: ${result.error ?? "see the activity log"}`);
  }
}

function finishJob(id: string, status: JobState["status"]): void {
  useApp.setState((s) => {
    const job = s.jobs[id];
    return job ? { jobs: { ...s.jobs, [id]: { ...job, progress: 1, status } } } : {};
  });
  setTimeout(() => {
    useApp.setState((s) => {
      const { [id]: _, ...rest } = s.jobs;
      return { jobs: rest };
    });
  }, 1500);
}

function handleEvent(event: EngineEvent): void {
  const app = useApp.getState();
  switch (event.method) {
    case "engine.status":
      useApp.setState({ engine: event.params });
      if (event.params.state === "crashed") app.toast("error", "The engine stopped unexpectedly and is restarting.");
      break;
    case "job.progress": {
      const { job_id, kind, progress, message } = event.params;
      useApp.setState((s) => ({
        jobs: { ...s.jobs, [job_id]: { id: job_id, kind, progress, message, status: "running" } },
      }));
      break;
    }
    case "job.done": {
      finishJob(event.params.job_id, "done");
      if (event.params.kind === "import") {
        const result = event.params.result as ImportResult;
        app.toast("success", `Imported ${result.clip_ids.length} file(s).`);
        app.invalidatePeaks();
        for (const p of result.problems.slice(0, 3)) app.toast("error", `Skipped ${p.path}: ${p.message}`);
        void app.refresh().then(() => useApp.getState().fit());
      } else if (event.params.kind === "sync") {
        const result = event.params.result as SyncRunResult;
        useApp.setState({
          timeline: result.timeline,
          lastSync: { pairs: result.pairs, reused: result.reused, matched: result.matched },
          matches: {},
        });
        app.invalidatePeaks();
        const { synced, clips } = result.timeline.stats;
        const review = result.timeline.review.length;
        app.toast("success", `Synchronised ${synced} of ${clips} clips${review ? `; ${review} to review` : ""}.`);
        void app.refresh().then(() => useApp.getState().fit());
      }
      break;
    }
    case "job.failed":
      finishJob(event.params.job_id, event.params.cancelled ? "cancelled" : "failed");
      if (event.params.cancelled) app.toast("info", `The ${event.params.kind} was cancelled.`);
      else app.toast("error", `The ${event.params.kind} failed: ${event.params.error}`);
      break;
    case "media.imported":
      break;
    case "media.analyzed":
      schedulePeaks();
      break;
    case "media.thumbnails":
      useThumbs.getState().arrived(event.params.thumbnails);
      break;
    case "pipeline.progress":
      useProd.getState().onPipeline(event.params);
      break;
    case "pipeline.matches":
      break;
    case "pipeline.error":
      app.toast("error", `Processing stopped: ${event.params.error}`);
      break;
    case "pipeline.sync_finished":
      void afterSync(event.params);
      break;
    case "menu":
      runMenu(event.params.command);
      break;
  }
}

function runMenu(command: MenuCommand): void {
  const app = useApp.getState();
  const hasProject = app.project !== null;
  switch (command) {
    case "new-project":
      void app.newProject();
      break;
    case "open-project":
      void app.openProject();
      break;
    case "close-project":
      if (hasProject) void app.closeProject();
      break;
    case "import-files":
      if (hasProject) void app.importMedia("files");
      break;
    case "import-folder":
      if (hasProject) void app.importMedia("folder");
      break;
    case "sync":
      if (hasProject) void app.sync();
      break;
    case "undo":
      if (hasProject && !isTyping()) void app.undo();
      else document.execCommand("undo");
      break;
    case "redo":
      if (hasProject && !isTyping()) void app.redo();
      else document.execCommand("redo");
      break;
    case "zoom-in":
      app.zoom(1.5);
      break;
    case "zoom-out":
      app.zoom(1 / 1.5);
      break;
    case "zoom-fit":
      app.fit();
      break;
    case "export":
      if (hasProject) app.openExport();
      break;
    case "settings":
      useProd.getState().openSettings();
      break;
  }
}

function isTyping(): boolean {
  const el = document.activeElement;
  return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}
