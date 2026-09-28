// The multicamera preview's settings: layout, program and active cameras, the audio monitor, preview quality, and
// which cameras are hidden, pinned or solo. Playback position lives in the master clock (features/multicam/clock).
import { create } from "zustand";

import type { SyncPoints } from "../api/contract";

export type Layout = "grid" | "program" | "compare";
/** Auto: the original where Chromium decodes it and there are few cameras, lighter pictures otherwise;
 * Performance: 360p pictures for every camera but the program; Quality: originals; Custom: one resolution. */
export type Quality = "auto" | "performance" | "quality" | "custom";
export const PREVIEW_HEIGHTS = [360, 540, 720, 1080] as const;

const KEY = "syncora.preview";

interface Saved {
  quality: Quality;
  customHeight: number;
  layout: "grid" | "program";
}

function load(): Saved {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<Saved>;
    return {
      quality: v.quality ?? "auto",
      customHeight: v.customHeight ?? 540,
      layout: v.layout === "program" ? "program" : "grid",
    };
  } catch {
    return { quality: "auto", customHeight: 540, layout: "grid" };
  }
}

function save(s: Saved): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    // this session only
  }
}

export interface TileStats {
  mode: "native" | "frames" | "none";
  /** Pictures shown per second over the last second. */
  fps: number;
  height: number | null;
}

export interface PlaybackState extends Omit<Saved, "layout"> {
  layout: Layout;
  /** The camera with the red focus (click, number keys): its clip is the one the inspector shows. */
  active: string | null;
  program: string | null;
  /** The camera whose sound is heard ("mute": none; null: automatic, the reference recorder). */
  monitor: string | null;
  hidden: Set<string>;
  pinned: string[];
  solo: string | null;
  page: number;
  /** Review: the reference camera on the left, the clip under review on the right. */
  compare: { reference: string; candidate: string } | null;
  stats: Record<string, TileStats>;
  /** Review workflow: the clips to review, in timeline order, and the one on screen. */
  review: { ids: number[]; index: number } | null;
  /** Sync points of the selected clip. */
  points: (SyncPoints & { clipId: number }) | null;

  setLayout(layout: Layout): void;
  setActive(key: string | null): void;
  setReview(review: PlaybackState["review"]): void;
  setPoints(points: PlaybackState["points"]): void;
  setProgram(key: string | null): void;
  setMonitor(key: string | null): void;
  setQuality(quality: Quality, customHeight?: number): void;
  toggleHidden(key: string): void;
  showAll(): void;
  togglePinned(key: string): void;
  setSolo(key: string | null): void;
  setPage(page: number): void;
  setCompare(compare: PlaybackState["compare"]): void;
  report(key: string, stats: TileStats): void;
}

export const usePlayback = create<PlaybackState>((set, get) => ({
  ...load(),
  program: null,
  monitor: null,
  hidden: new Set(),
  pinned: [],
  solo: null,
  page: 0,
  compare: null,
  stats: {},
  active: null,
  review: null,
  points: null,

  setLayout(layout) {
    set({ layout });
    if (layout !== "compare") save({ quality: get().quality, customHeight: get().customHeight, layout });
  },
  setProgram(program) {
    set({ program });
  },
  setActive(active) {
    set({ active });
  },
  setReview(review) {
    set({ review });
  },
  setPoints(points) {
    set({ points });
  },
  setMonitor(monitor) {
    set({ monitor });
  },
  setQuality(quality, customHeight) {
    set({ quality, ...(customHeight ? { customHeight } : {}) });
    const s = get();
    save({ quality: s.quality, customHeight: s.customHeight, layout: s.layout === "program" ? "program" : "grid" });
  },
  toggleHidden(key) {
    set((s) => {
      const hidden = new Set(s.hidden);
      if (hidden.has(key)) hidden.delete(key);
      else hidden.add(key);
      return { hidden, solo: s.solo === key ? null : s.solo };
    });
  },
  showAll() {
    set({ hidden: new Set(), solo: null });
  },
  togglePinned(key) {
    set((s) => ({ pinned: s.pinned.includes(key) ? s.pinned.filter((k) => k !== key) : [...s.pinned, key] }));
  },
  setSolo(solo) {
    set({ solo });
  },
  setPage(page) {
    set({ page });
  },
  setCompare(compare) {
    set({ compare, layout: compare ? "compare" : load().layout });
  },
  report(key, stats) {
    const old = get().stats[key];
    if (old && old.mode === stats.mode && old.fps === stats.fps && old.height === stats.height) return;
    set((s) => ({ stats: { ...s.stats, [key]: stats } }));
  },
}));

/** The picture height a camera window asks for (frames mode), from the quality setting and the window's size. */
export function previewHeight(quality: Quality, customHeight: number, tileHeight: number, program: boolean): number {
  if (quality === "custom") return customHeight;
  if (quality === "performance") return program ? 540 : 360;
  const wanted = Math.max(360, Math.min(1080, Math.round((tileHeight * (window.devicePixelRatio || 1)) / 90) * 90));
  return quality === "quality" ? Math.max(720, wanted) : wanted;
}
