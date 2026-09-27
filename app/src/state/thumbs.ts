// Poster frames for media cards: requested only for cards on screen, kept as object URLs in a bounded cache.
import { create } from "zustand";

import { bridge, call } from "../api/client";

const MAX_URLS = 1500;

interface ThumbState {
  urls: Map<number, string>;
  want(clipIds: number[]): void;
  arrived(pairs: [number, string][]): void;
  clear(): void;
}

const requested = new Set<number>();
let queue: number[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;

export const useThumbs = create<ThumbState>((set, get) => ({
  urls: new Map(),

  want(clipIds) {
    const missing = clipIds.filter((id) => !get().urls.has(id) && !requested.has(id));
    if (!missing.length) return;
    for (const id of missing) requested.add(id);
    queue.push(...missing);
    if (timer) return;
    timer = setTimeout(() => {
      timer = null;
      const batch = queue.splice(0, 400);
      void call("media.thumbnails", { clip_ids: batch }).then(
        (r) => get().arrived(r.thumbnails),
        () => batch.forEach((id) => requested.delete(id)),
      );
    }, 120);
  },

  arrived(pairs) {
    void Promise.all(
      pairs.map(async ([id, path]) => {
        try {
          const bytes = await bridge().readThumbnail(path);
          return [id, URL.createObjectURL(new Blob([bytes as BlobPart], { type: "image/png" }))] as const;
        } catch {
          return null;
        }
      }),
    ).then((loaded) => {
      set((s) => {
        const urls = new Map(s.urls);
        for (const item of loaded) if (item) urls.set(item[0], item[1]);
        // Least recently added go first once the cache is full.
        while (urls.size > MAX_URLS) {
          const [oldest, url] = urls.entries().next().value as [number, string];
          URL.revokeObjectURL(url);
          urls.delete(oldest);
          requested.delete(oldest);
        }
        return { urls };
      });
    });
  },

  clear() {
    for (const url of get().urls.values()) URL.revokeObjectURL(url);
    requested.clear();
    queue = [];
    set({ urls: new Map() });
  },
}));
