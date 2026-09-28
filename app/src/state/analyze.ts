// Stage 03 Analyze (S09 transcript, speakers and markers; S10 search) and S07 AI sync: what the AI features show.
// Kept apart from the timeline and production stores; engine notifications reload only what changed.
import { create } from "zustand";

import { call } from "../api/client";
import type {
  AiStatus,
  AiSyncResult,
  Marker,
  SearchHit,
  Speaker,
  Transcript,
  TranscriptsOverview,
} from "../api/contract";
import { useProd } from "./production";
import { useApp } from "./store";

export type AnalyzeTab = "overview" | "transcript" | "speakers" | "markers" | "search";

export interface AnalyzeState {
  tab: AnalyzeTab;
  status: AiStatus | null;
  overview: TranscriptsOverview | null;
  speakers: Speaker[];
  markers: Marker[];
  /** The clip the transcript view shows. */
  clipId: number | null;
  transcript: Transcript | null;
  /** Where the transcript player should seek to (a new token each time, so the same time can be asked again). */
  seek: { t: number; token: number } | null;
  query: string;
  results: SearchHit[] | null;
  searching: boolean;
  activeHit: number;
  /** S07: the clip AI sync looks for, its job and its result. */
  aiClip: number | null;
  aiJob: string | null;
  aiResult: AiSyncResult | null;
  aiCandidate: number;

  reset(): void;
  setTab(tab: AnalyzeTab): void;
  loadStatus(): Promise<void>;
  loadOverview(): Promise<void>;
  loadSpeakers(): Promise<void>;
  loadMarkers(): Promise<void>;
  openClip(clipId: number, t?: number): Promise<void>;
  reloadTranscript(): Promise<void>;
  transcribe(scope: "smart" | "all" | "clips", clipIds?: number[], redo?: boolean): Promise<void>;
  cancelTranscription(clipIds?: number[]): Promise<void>;
  renameSpeaker(key: string, name: string | null): Promise<void>;
  mergeSpeakers(keys: string[], into: string): Promise<void>;
  addMarker(clipId: number, t: number, label?: string | null, source?: "user" | "search"): Promise<void>;
  renameMarker(id: number, label: string | null): Promise<void>;
  deleteMarker(id: number): Promise<void>;
  search(text: string): Promise<void>;
  setQuery(text: string): void;
  setActiveHit(k: number): void;

  openAiSync(clipId: number): Promise<void>;
  startAiSync(): Promise<void>;
  onAiJobDone(jobId: string, failed: boolean): Promise<void>;
  setCandidate(k: number): void;
  acceptAi(): Promise<void>;
  rejectAi(): Promise<void>;
}

function run<T>(action: () => Promise<T>): Promise<T | undefined> {
  return useApp.getState().run(action);
}

let seekToken = 0;
let searchToken = 0;
let reloadTimer: ReturnType<typeof setTimeout> | null = null;
let reloadWanted = { transcript: false, lists: false };
const RELOAD_MS = 1200;

export const useAnalyze = create<AnalyzeState>((set, get) => ({
  tab: "overview",
  status: null,
  overview: null,
  speakers: [],
  markers: [],
  clipId: null,
  transcript: null,
  seek: null,
  query: "",
  results: null,
  searching: false,
  activeHit: 0,
  aiClip: null,
  aiJob: null,
  aiResult: null,
  aiCandidate: 0,

  reset() {
    set({
      tab: "overview",
      overview: null,
      speakers: [],
      markers: [],
      clipId: null,
      transcript: null,
      seek: null,
      query: "",
      results: null,
      activeHit: 0,
      aiClip: null,
      aiJob: null,
      aiResult: null,
      aiCandidate: 0,
    });
    if (useApp.getState().project) {
      void get().loadStatus();
      void get().loadOverview();
      void get().loadSpeakers();
      void get().loadMarkers();
    }
  },

  setTab(tab) {
    set({ tab });
    if (tab === "overview") void get().loadOverview();
    if (tab === "markers") void get().loadMarkers();
    if (tab === "speakers") void get().loadSpeakers();
    if (tab === "transcript" && get().clipId === null) {
      const first = get().overview?.clips.find((c) => c.done > 0) ?? get().overview?.clips[0];
      if (first) void get().openClip(first.clip_id);
    }
  },

  async loadStatus() {
    try {
      set({ status: await call("ai.status", {}) });
    } catch {
      // engine restarting
    }
  },
  async loadOverview() {
    if (!useApp.getState().project) return;
    try {
      set({ overview: await call("transcripts.overview", {}) });
    } catch {
      // project closed meanwhile
    }
  },
  async loadSpeakers() {
    if (!useApp.getState().project) return;
    try {
      set({ speakers: await call("speakers.list", {}) });
    } catch {
      // project closed meanwhile
    }
  },
  async loadMarkers() {
    if (!useApp.getState().project) return;
    try {
      set({ markers: await call("markers.list", {}) });
    } catch {
      // project closed meanwhile
    }
  },

  async openClip(clipId, t) {
    const same = get().clipId === clipId;
    set({
      clipId,
      tab: "transcript",
      transcript: same ? get().transcript : null,
      seek: t !== undefined ? { t, token: ++seekToken } : same ? get().seek : null,
    });
    if (!same) await get().reloadTranscript();
  },

  async reloadTranscript() {
    const clipId = get().clipId;
    if (clipId === null) return;
    try {
      const transcript = await call("transcript.get", { clip_id: clipId });
      if (get().clipId === clipId) set({ transcript });
    } catch {
      if (get().clipId === clipId) set({ transcript: null });
    }
  },

  async transcribe(scope, clipIds, redo = false) {
    await run(async () => {
      const r = await call("transcripts.start", { scope, clip_ids: clipIds, redo });
      useApp
        .getState()
        .toast(
          "info",
          r.clips
            ? `Transcribing ${r.clips} recording${r.clips === 1 ? "" : "s"} on this computer (${r.tasks} part${r.tasks === 1 ? "" : "s"}).`
            : "Everything in scope is transcribed already.",
        );
      await get().loadOverview();
    });
  },
  async cancelTranscription(clipIds) {
    await run(async () => {
      const r = await call("transcripts.cancel", clipIds ? { clip_ids: clipIds } : {});
      useApp.getState().toast("info", `Cancelled ${r.cancelled} transcription part(s). Finished parts are kept.`);
      await get().loadOverview();
    });
  },

  async renameSpeaker(key, name) {
    await run(async () => {
      set({ speakers: await call("speakers.rename", { key, name }) });
      await get().reloadTranscript();
    });
  },
  async mergeSpeakers(keys, into) {
    await run(async () => {
      set({ speakers: await call("speakers.merge", { keys, into }) });
      await get().reloadTranscript();
      useApp.getState().toast("success", "Speakers merged.");
    });
  },

  async addMarker(clipId, t, label = null, source = "user") {
    await run(async () => {
      await call("markers.add", { clip_id: clipId, t_s: t, label, source });
      useApp.getState().toast("success", "Marker added.");
      await Promise.all([get().loadMarkers(), get().clipId === clipId ? get().reloadTranscript() : null]);
    });
  },
  async renameMarker(id, label) {
    await run(async () => {
      await call("markers.update", { marker_id: id, label });
      await Promise.all([get().loadMarkers(), get().reloadTranscript()]);
    });
  },
  async deleteMarker(id) {
    await run(async () => {
      await call("markers.delete", { marker_id: id });
      await Promise.all([get().loadMarkers(), get().reloadTranscript()]);
    });
  },

  setQuery(query) {
    set({ query });
  },
  async search(text) {
    const token = ++searchToken;
    set({ query: text, searching: true });
    if (!text.trim()) {
      set({ results: null, searching: false, activeHit: 0 });
      return;
    }
    try {
      const r = await call("search.query", { text, limit: 100 });
      if (token === searchToken) set({ results: r.results, activeHit: 0 });
    } catch (err) {
      if (token === searchToken) set({ results: [] });
      useApp.getState().toast("error", err instanceof Error ? err.message : String(err));
    } finally {
      if (token === searchToken) set({ searching: false });
    }
  },
  setActiveHit(activeHit) {
    set({ activeHit });
  },

  // ------------------------------------------------------------------ S07 AI sync

  async openAiSync(clipId) {
    set({ aiClip: clipId, aiResult: null, aiCandidate: 0, aiJob: get().aiClip === clipId ? get().aiJob : null });
    const prod = useProd.getState();
    prod.setStage("sync");
    prod.setSyncView("ai");
    try {
      const result = await call("ai.sync_result", { clip_id: clipId });
      if (get().aiClip !== clipId) return;
      set({ aiResult: result });
      // A new search, unless one is running or a proposal is waiting for a decision.
      if (!result && !get().aiJob) await get().startAiSync();
    } catch (err) {
      useApp.getState().toast("error", err instanceof Error ? err.message : String(err));
    }
  },

  async startAiSync() {
    const clipId = get().aiClip;
    if (clipId === null) return;
    await run(async () => {
      const { job_id } = await call("ai.sync", { clip_id: clipId });
      set({ aiJob: job_id, aiResult: null, aiCandidate: 0 });
    });
  },

  async onAiJobDone(jobId, failed) {
    if (jobId !== get().aiJob) return;
    const clipId = get().aiClip;
    set({ aiJob: null });
    if (clipId === null) return;
    if (failed) return;
    try {
      const result = await call("ai.sync_result", { clip_id: clipId });
      if (get().aiClip === clipId) set({ aiResult: result, aiCandidate: 0 });
    } catch {
      // project closed meanwhile
    }
  },

  setCandidate(aiCandidate) {
    set({ aiCandidate });
  },

  async acceptAi() {
    const { aiClip, aiCandidate } = get();
    if (aiClip === null) return;
    await run(async () => {
      const timeline = await call("ai.sync_accept", { clip_id: aiClip, candidate: aiCandidate });
      useApp.getState().adoptTimeline(timeline);
      useApp.getState().toast("success", "Placed where AI sync found it. Undo takes it back.");
      set({ aiClip: null, aiResult: null });
      useProd.getState().setSyncView("results");
      useProd.getState().setStage("timeline");
      useApp.getState().select(aiClip);
      useApp.getState().reveal(aiClip);
      void useProd.getState().loadSummary();
      void useProd.getState().loadIndex();
    });
  },

  async rejectAi() {
    const { aiClip, aiJob } = get();
    if (aiClip === null) return;
    await run(async () => {
      if (aiJob) await call("job.cancel", { job_id: aiJob });
      const timeline = await call("ai.sync_reject", { clip_id: aiClip });
      useApp.getState().adoptTimeline(timeline);
      set({ aiClip: null, aiResult: null, aiJob: null });
      useProd.getState().setSyncView("results");
      useProd.getState().setStage("timeline");
      useApp.getState().select(aiClip);
      useApp.getState().reveal(aiClip);
    });
  },
}));

/** Transcripts changed (a transcription part finished): reload what shows them, at most about once a second. */
export function transcriptsChanged(clipIds: number[]): void {
  const state = useAnalyze.getState();
  const shown = state.clipId;
  if (shown !== null) {
    const own = clipIds.includes(shown);
    const shared = state.transcript?.segments.some((s) => clipIds.includes(s.source_clip_id)) ?? false;
    if (own || shared || !state.transcript?.segments.length) reloadWanted.transcript = true;
  }
  reloadWanted.lists = true;
  if (reloadTimer) return;
  reloadTimer = setTimeout(() => {
    reloadTimer = null;
    const wanted = reloadWanted;
    reloadWanted = { transcript: false, lists: false };
    const a = useAnalyze.getState();
    if (wanted.transcript) void a.reloadTranscript();
    if (wanted.lists) {
      void a.loadOverview();
      void a.loadSpeakers();
      void a.loadMarkers();
    }
  }, RELOAD_MS);
}

/** Swatch colours of speakers, by their position in the list (Transcript spec §1.3: cycle after 4). */
export const SPEAKER_SWATCHES = [
  "var(--sy-speaker-1)",
  "var(--sy-speaker-2)",
  "var(--sy-speaker-3)",
  "var(--sy-speaker-4)",
];

/** "S03" → "Speaker 03". */
export function speakerLabel(key: string | null): string {
  if (!key) return "Unknown speaker";
  const n = /^S(\d+)$/.exec(key);
  return n ? `Speaker ${n[1]}` : key;
}

export function speakerName(key: string | null, name: string | null | undefined): string {
  return name?.trim() || speakerLabel(key);
}
