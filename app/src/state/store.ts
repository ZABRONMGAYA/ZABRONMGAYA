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
import { defaultPresetId, presetById } from "../lib/presets";
import { clock } from "../features/multicam/clock";
import { type View, anchorClip, clampView, fitView, offsetForStart, zoomAround } from "../features/timeline/geometry";
import { forgetPeaks } from "../features/timeline/peaks";
import { transcriptsChanged, useAnalyze } from "./analyze";
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
  /** Corrections are on their way to the engine (the timeline shows them already). */
  updating: boolean;

  init(): () => void;
  toast(kind: Toast["kind"], text: string): void;
  dismiss(id: number): void;
  run<T>(action: () => Promise<T>): Promise<T | undefined>;

  /** Create a project with a preset's settings (the default preset when none is given). */
  newProject(presetId?: string): Promise<void>;
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
  /** Move a clip by `deltaS` at once; rapid nudges are sent to the engine as one correction when they stop. */
  nudge(clipId: number, deltaS: number): void;
  snap(clipId: number): Promise<void>;
  loadMatches(clipId: number): Promise<void>;
  undo(): Promise<void>;
  redo(): Promise<void>;
  setReference(clipId: number | null): Promise<void>;
  /** Show a timeline the engine returned from another request (AI sync accepted or rejected). */
  adoptTimeline(timeline: Timeline): void;

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

/** Clips by id, built once per timeline (lists of thousands of clips look clips up for every row). */
const clipIndex = new WeakMap<Timeline, Map<number, TimelineClip>>();

export function findClip(timeline: Timeline | null, clipId: number | null): TimelineClip | undefined {
  if (!timeline || clipId === null) return undefined;
  let index = clipIndex.get(timeline);
  if (!index) {
    index = new Map();
    for (const g of timeline.groups) for (const c of g.clips) index.set(c.clip_id, c);
    for (const c of timeline.unsynced) index.set(c.clip_id, c);
    clipIndex.set(timeline, index);
  }
  return index.get(clipId);
}

// ------------------------------------------------------------- moves shown before the engine confirms them
//
// A drag or a nudge moves the clip on screen at once. The engine re-solves the timeline (up to a second on a
// project of thousands of clips) and its answer replaces the preview. A move is kept as an offset from the clip
// the timeline is anchored on, so it stays right when the engine's answer shifts the timeline's origin.

interface PendingMove {
  anchorId: number;
  offsetS: number;
  token: number;
  sent: boolean;
}

const NUDGE_SETTLE_MS = 350;
const pendingMoves = new Map<number, PendingMove>();
let moveToken = 0;
let nudgeTimer: ReturnType<typeof setTimeout> | null = null;
let inFlight = 0;
/** The timeline as the engine last sent it, before pending moves are applied, and what the store shows for it. */
let engineTimeline: Timeline | null = null;
let shownTimeline: Timeline | null = null;

/** The engine's timeline with the moves it has not confirmed yet. */
function withPendingMoves(timeline: Timeline | null): Timeline | null {
  if (!timeline || pendingMoves.size === 0) return timeline;
  const starts = new Map<number, number>();
  for (const [clipId, move] of pendingMoves) {
    const clip = findClip(timeline, clipId);
    const anchor = findClip(timeline, move.anchorId);
    if (clip?.group == null || anchor?.start_s == null || anchor.group !== clip.group) continue;
    starts.set(clipId, anchor.start_s + move.offsetS);
  }
  if (starts.size === 0) return timeline;
  const groups = timeline.groups.map((g) =>
    g.clips.some((c) => starts.has(c.clip_id))
      ? { ...g, clips: g.clips.map((c) => (starts.has(c.clip_id) ? { ...c, start_s: starts.get(c.clip_id)! } : c)) }
      : g,
  );
  return { ...timeline, groups };
}

/** What the store shows for a timeline from the engine. */
function showTimeline(timeline: Timeline | null): Timeline | null {
  engineTimeline = timeline;
  shownTimeline = withPendingMoves(timeline);
  return shownTimeline;
}

/** Show the pending moves again over the engine's timeline (the store's own, if it was set from elsewhere). */
function reshow(): void {
  const current = useApp.getState().timeline;
  useApp.setState({ timeline: showTimeline(current === shownTimeline ? engineTimeline : current) });
}

const REFRESH_INTERVAL_MS = 4000;
let refreshedVersion: number | null = null;
let refreshing: Promise<void> | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let lastRefresh = 0;

/**
 * Reload the timeline when the media changed since it was last loaded (an import running in the background, or
 * a project created and filled since). At most one reload every few seconds, whatever the project size.
 */
export function refreshIfStale(): void {
  const { project } = useApp.getState();
  if (!project || refreshTimer) return;
  const version = useProd.getState().indexVersion;
  if (engineTimeline !== null && version === refreshedVersion) return;
  const wait = refreshing ? REFRESH_INTERVAL_MS : Math.max(0, REFRESH_INTERVAL_MS - (Date.now() - lastRefresh));
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    if (refreshing) {
      refreshIfStale();
      return;
    }
    lastRefresh = Date.now();
    refreshing = useApp
      .getState()
      .refresh()
      .catch(() => undefined) // the project closed meanwhile
      .finally(() => {
        refreshing = null;
        if (useProd.getState().indexVersion !== refreshedVersion) refreshIfStale();
      });
  }, wait);
}

function markUpdating(): void {
  const updating = inFlight > 0 || pendingMoves.size > 0;
  if (useApp.getState().updating !== updating) useApp.setState({ updating });
}

/** Count an engine request that changes the timeline, for the "Updating…" note. */
async function tracked<T>(request: () => Promise<T>): Promise<T> {
  inFlight += 1;
  markUpdating();
  try {
    return await request();
  } finally {
    inFlight -= 1;
    markUpdating();
  }
}

function forgetMoves(): void {
  if (nudgeTimer) clearTimeout(nudgeTimer);
  nudgeTimer = null;
  pendingMoves.clear();
}

/** Show `clipId` at `startS` now; the engine hears of it when the moves are flushed. */
function stageMove(clipId: number, anchor: TimelineClip, startS: number): void {
  pendingMoves.set(clipId, {
    anchorId: anchor.clip_id,
    offsetS: offsetForStart(startS, anchor),
    token: ++moveToken,
    sent: false,
  });
  reshow();
  markUpdating();
}

/** Send the moves not sent yet, and wait for the engine's timeline. */
async function flushMoves(): Promise<void> {
  if (nudgeTimer) clearTimeout(nudgeTimer);
  nudgeTimer = null;
  const unsent = [...pendingMoves].filter(([, move]) => !move.sent);
  await Promise.all(
    unsent.map(([clipId, move]) => {
      move.sent = true;
      return sendCorrection(
        { kind: "offset", clip_id: clipId, other_clip_id: move.anchorId, offset_s: move.offsetS },
        {
          clipId,
          token: move.token,
        },
      );
    }),
  );
}

function scheduleFlush(): void {
  if (nudgeTimer) clearTimeout(nudgeTimer);
  nudgeTimer = setTimeout(() => void flushMoves(), NUDGE_SETTLE_MS);
}

/** Add a correction and show the engine's new timeline. A failed move is taken back off the screen. */
async function sendCorrection(
  params: { kind: CorrectionKind; clip_id: number; other_clip_id?: number; offset_s?: number },
  move?: { clipId: number; token: number },
): Promise<void> {
  const app = useApp.getState();
  const project = app.project?.path;
  const settle = () => {
    if (move && pendingMoves.get(move.clipId)?.token === move.token) pendingMoves.delete(move.clipId);
  };
  await tracked(() =>
    app.run(async () => {
      try {
        const timeline = await call("correction.add", params);
        if (useApp.getState().project?.path !== project) return; // another project was opened meanwhile
        settle();
        useApp.setState({ timeline: showTimeline(timeline), matches: {} });
      } catch (err) {
        settle();
        reshow();
        throw err;
      }
      const selected = useApp.getState().selected;
      if (selected !== null) void useApp.getState().loadMatches(selected);
    }),
  );
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
  updating: false,

  init() {
    const off = bridge().onEvent((event) => handleEvent(event));
    void bridge()
      .engineStatus()
      .then((engine) => {
        set({ engine });
        if (engine.state === "ready" && wasReloaded()) void adoptOpenProject();
      });
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

  async newProject(presetId) {
    const path = await bridge().chooseProjectToCreate("Untitled project");
    if (!path) return;
    await get().run(async () => {
      let project = await call("project.create", { path });
      const preset = presetById(presetId ?? defaultPresetId());
      if (Object.keys(preset.settings).length) {
        project = { ...project, settings: await call("project.update_settings", preset.settings) };
      }
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
    await flushMoves();
    await get().run(async () => {
      await call("project.close", {});
      forgetMoves();
      set({
        project: null,
        timeline: showTimeline(null),
        media: { clips: [], devices: [] },
        selected: null,
        lastSync: null,
      });
      useProd.getState().reset(null);
      useAnalyze.getState().reset();
    });
  },

  async refresh() {
    if (!get().project) return;
    const version = useProd.getState().indexVersion;
    const [media, timeline, project] = await Promise.all([
      call("media.list", {}),
      call("timeline.get", {}),
      call("project.info", {}),
    ]);
    if (get().project?.path !== project.path) return; // another project was opened meanwhile
    refreshedVersion = version;
    set({ media, timeline: showTimeline(timeline), project });
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
    await flushMoves(); // corrections apply in the order they were made
    await sendCorrection({
      kind,
      clip_id: clipId,
      ...(otherClipId !== undefined ? { other_clip_id: otherClipId } : {}),
      ...(offsetS !== undefined ? { offset_s: offsetS } : {}),
    });
  },

  async moveClip(clipId, startS) {
    const anchor = movableAnchor(clipId);
    if (!anchor) return;
    stageMove(clipId, anchor, startS);
    await flushMoves();
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

  nudge(clipId, deltaS) {
    // The timeline already shows earlier nudges, so rapid key presses add up.
    const clip = findClip(get().timeline, clipId);
    if (clip?.start_s === null || clip?.start_s === undefined) return;
    const anchor = movableAnchor(clipId);
    if (!anchor) return;
    stageMove(clipId, anchor, clip.start_s + deltaS);
    scheduleFlush();
  },

  async snap(clipId) {
    await flushMoves();
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
    await flushMoves();
    await get().run(() =>
      tracked(async () => set({ timeline: showTimeline(await call("correction.undo", {})), matches: {} })),
    );
  },

  async redo() {
    await flushMoves();
    await get().run(() =>
      tracked(async () => set({ timeline: showTimeline(await call("correction.redo", {})), matches: {} })),
    );
  },

  adoptTimeline(timeline) {
    set({ timeline: showTimeline(timeline), matches: {} });
    const selected = get().selected;
    if (selected !== null) void get().loadMatches(selected);
  },

  async setReference(clipId) {
    await flushMoves();
    await get().updateSettings({ reference_clip_id: clipId });
    await get().run(() => tracked(async () => set({ timeline: showTimeline(await call("sync.solve", {})) })));
  },

  // ------------------------------------------------------------ export

  openExport() {
    const open = () => {
      if (!get().timeline?.groups.length) {
        get().toast("info", "Synchronise first: there is no timeline to export yet.");
        return;
      }
      set({ exportOpen: true, lastExport: null });
    };
    // A project created and synchronised in this session may not have loaded its timeline yet.
    if (get().timeline || !get().project) {
      open();
      return;
    }
    void get().run(async () => {
      await get().refresh();
      open();
    });
  },

  closeExport() {
    set({ exportOpen: false });
  },

  async exportXml(options) {
    const project = get().project;
    if (!project) return undefined;
    await flushMoves(); // export what the timeline shows
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
    // The edit cursor is the playhead of the master clock (viewer, audio monitor, timecode).
    if (timeS !== null && Math.abs(clock.now() - timeS) > 1e-6) clock.seek(timeS);
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

/** The clip of the displayed group that `clipId` is moved relative to, or undefined when it cannot be moved. */
function movableAnchor(clipId: number): TimelineClip | undefined {
  const state = useApp.getState();
  const g = displayedGroup(state);
  const anchor = g && anchorClip(g, state.timeline!.reference_clip_id);
  if (!anchor) return undefined;
  if (anchor.clip_id === clipId) {
    state.toast("info", "The reference clip defines the timeline; move the other clips instead.");
    return undefined;
  }
  return anchor;
}

function clamped(view: View): View {
  const { timelineWidth } = useApp.getState();
  const g = displayedGroup(useApp.getState());
  return g ? clampView(view, g.duration_s, timelineWidth) : view;
}

function afterOpen(project: ProjectSummary): void {
  const recent = [project.path, ...useApp.getState().recent.filter((p) => p !== project.path)];
  saveRecent(recent);
  forgetMoves();
  refreshedVersion = null;
  useApp.setState({
    project,
    recent,
    timeline: showTimeline(null),
    updating: false,
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
  useAnalyze.getState().reset();
}

/** The window was reloaded (after a renderer crash, or Reload window), not opened. */
function wasReloaded(): boolean {
  const [entry] = performance.getEntriesByType?.("navigation") ?? [];
  return (entry as PerformanceNavigationTiming | undefined)?.type === "reload";
}

/** After the window was reloaded, carry on with the project the engine still has open. */
async function adoptOpenProject(): Promise<void> {
  try {
    const project = await call("project.info", {});
    if (useApp.getState().project) return;
    afterOpen(project);
    await useApp.getState().refresh();
    useApp.getState().fit();
  } catch {
    // no project is open: the window starts on Home
  }
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
    if (result.timeline) useApp.setState({ timeline: showTimeline(result.timeline), matches: {} });
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
          timeline: showTimeline(result.timeline),
          lastSync: { pairs: result.pairs, reused: result.reused, matched: result.matched },
          matches: {},
        });
        app.invalidatePeaks();
        const { synced, clips } = result.timeline.stats;
        const review = result.timeline.review.length;
        app.toast("success", `Synchronised ${synced} of ${clips} clips${review ? `; ${review} to review` : ""}.`);
        void app.refresh().then(() => useApp.getState().fit());
      } else if (event.params.kind === "ai-sync") {
        void useAnalyze.getState().onAiJobDone(event.params.job_id, false);
      } else if (event.params.kind === "model-download") {
        app.toast("success", "Model downloaded: it works offline from now on.");
        void useAnalyze.getState().loadStatus();
      }
      break;
    }
    case "job.failed": {
      finishJob(event.params.job_id, event.params.cancelled ? "cancelled" : "failed");
      const what = JOB_LABELS[event.params.kind] ?? event.params.kind;
      if (event.params.cancelled) app.toast("info", `${capitalise(what)} was cancelled.`);
      else app.toast("error", `${capitalise(what)} failed: ${event.params.error}`);
      if (event.params.kind === "ai-sync") void useAnalyze.getState().onAiJobDone(event.params.job_id, true);
      if (event.params.kind === "model-download") void useAnalyze.getState().loadStatus();
      break;
    }
    case "transcript.updated":
      transcriptsChanged(event.params.clip_ids);
      break;
    case "ai.fallback_done": {
      const { clips, placed } = event.params;
      if (clips)
        app.toast(
          placed ? "success" : "info",
          placed
            ? `AI sync proposed a place for ${placed} of ${clips} clip${clips === 1 ? "" : "s"} audio could not place: check them in Review.`
            : `AI sync found no reliable place for the ${clips} clip${clips === 1 ? "" : "s"} audio could not place.`,
        );
      void app.refresh();
      void useProd.getState().loadSummary();
      break;
    }
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
    case "search":
      if (hasProject) openSearch();
      break;
  }
}

/** S10 with the cursor in the search field (⌘K). */
export function openSearch(): void {
  useProd.getState().setStage("analyze");
  useAnalyze.getState().setTab("search");
  requestAnimationFrame(() => document.querySelector<HTMLInputElement>("[data-testid=moment-search]")?.focus());
}

const JOB_LABELS: Record<string, string> = {
  import: "the import",
  sync: "the sync",
  "ai-sync": "the AI sync search",
  "ai-fallback": "the AI fallback",
  "model-download": "the model download",
  rescan: "the rescan",
};

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function isTyping(): boolean {
  const el = document.activeElement;
  return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}
