// Store behaviour against a fake engine bridge.
import { beforeEach, describe, expect, it } from "vitest";

import type { Bridge, EngineMethods, Method, Timeline } from "../src/api/contract";
import { findClip, useApp } from "../src/state/store";
import { clip, group } from "./fixtures";

type Handler = (params: unknown) => unknown;

function timeline(cameraStart: number): Timeline {
  const recorder = clip(2, 0, 600, { method: "reference", kind: "recorder", has_video: false });
  const camera = clip(1, cameraStart, 100);
  return {
    groups: [group([camera, recorder])],
    unsynced: [],
    review: [],
    reference_clip_id: 2,
    stats: { clips: 2, synced: 2, needs_review: 0, unsynced: 0 },
  };
}

function installBridge(handlers: Partial<Record<Method, Handler>>): unknown[][] {
  const calls: unknown[][] = [];
  const bridge: Bridge = {
    async invoke<M extends Method>(method: M, params: EngineMethods[M][0]) {
      calls.push([method, params]);
      const handler = handlers[method];
      if (!handler) return { ok: false as const, error: { code: -32601, message: `no ${method}` } };
      return { ok: true as const, result: (await handler(params)) as EngineMethods[M][1] };
    },
    onEvent: () => () => {},
    engineStatus: async () => ({ state: "ready", hello: null, error: null }),
    chooseMedia: async () => [],
    chooseProjectToOpen: async () => null,
    chooseProjectToCreate: async () => null,
    chooseFolder: async () => null,
    chooseFile: async () => null,
    saveReport: async () => null,
    chooseExportPath: async () => null,
    showInFolder: async () => {},
    pathForFile: () => "",
    readPeaks: async () => new Uint8Array(),
    readThumbnail: async () => new Uint8Array(),
    preview: {
      caps: async () => ({ hwaccels: [], ffmpeg: "ffmpeg", videoDecode: "enabled" }),
      frame: async () => null,
      open: async () => "s1",
      at: async () => null,
      close: async () => undefined,
      audio: async () => null,
    },
    mediaUrl: (file: string) => `syncora-media://media/${encodeURIComponent(file)}`,
    platform: "test",
  };
  window.mcsync = bridge;
  return calls;
}

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));
const shownStart = (clipId = 1) => findClip(useApp.getState().timeline, clipId)!.start_s;
const offsetsSent = (calls: unknown[][]) =>
  calls.filter(([m]) => m === "correction.add").map(([, p]) => (p as { offset_s: number }).offset_s.toFixed(3));

beforeEach(() => {
  useApp.setState({ timeline: timeline(10), group: 0, selected: 1, toasts: [], matches: {}, snaps: {} });
});

describe("corrections", () => {
  it("shows rapid nudges at once and sends them as one correction when they stop", async () => {
    const calls = installBridge({
      "correction.add": async (p) => {
        await delay(20);
        return timeline((p as { offset_s: number }).offset_s);
      },
      "sync.matches": () => [],
    });
    const { nudge } = useApp.getState();
    nudge(1, 0.04);
    nudge(1, 0.04);
    nudge(1, 0.04);
    expect(shownStart()).toBeCloseTo(10.12, 9);
    expect(useApp.getState().updating).toBe(true);
    expect(offsetsSent(calls)).toEqual([]);

    await delay(450);
    expect(offsetsSent(calls)).toEqual(["10.120"]);
    expect(shownStart()).toBeCloseTo(10.12, 9);
    expect(useApp.getState().updating).toBe(false);
  });

  it("moves a dragged clip at once and takes it back when the engine refuses", async () => {
    installBridge({});
    const moved = useApp.getState().moveClip(1, 42);
    expect(shownStart()).toBe(42);
    await moved;
    expect(shownStart()).toBe(10);
    expect(useApp.getState().toasts.at(-1)).toMatchObject({ kind: "error", text: "no correction.add" });
    expect(useApp.getState().updating).toBe(false);
  });

  it("keeps showing a nudge made while an earlier move is on its way", async () => {
    const calls = installBridge({
      "correction.add": async (p) => {
        await delay(30);
        return timeline((p as { offset_s: number }).offset_s);
      },
      "sync.matches": () => [],
    });
    const moved = useApp.getState().moveClip(1, 42);
    useApp.getState().nudge(1, 1);
    expect(shownStart()).toBe(43);
    await moved; // the engine's timeline for the drag (42) arrives; the nudge is not sent yet
    expect(shownStart()).toBe(43);
    await delay(450);
    expect(offsetsSent(calls)).toEqual(["42.000", "43.000"]);
    expect(shownStart()).toBe(43);
  });

  it("sends waiting nudges before an undo", async () => {
    const calls = installBridge({
      "correction.add": (p) => timeline((p as { offset_s: number }).offset_s),
      "correction.undo": () => timeline(10),
      "sync.matches": () => [],
    });
    useApp.getState().nudge(1, 0.5);
    await useApp.getState().undo();
    expect(calls.map(([m]) => m).filter((m) => m !== "sync.matches")).toEqual(["correction.add", "correction.undo"]);
    expect(shownStart()).toBe(10);
  });

  it("finds clips by id on the timeline and among unplaced clips", () => {
    const t = timeline(10);
    const unplaced = clip(3, null, 50);
    const withUnplaced = { ...t, unsynced: [unplaced] };
    expect(findClip(withUnplaced, 1)?.start_s).toBe(10);
    expect(findClip(withUnplaced, 3)).toBe(unplaced);
    expect(findClip(withUnplaced, 99)).toBeUndefined();
    expect(findClip(null, 1)).toBeUndefined();
  });

  it("moves clips relative to the reference and refuses to move the reference", async () => {
    const calls = installBridge({ "correction.add": () => timeline(42), "sync.matches": () => [] });
    await useApp.getState().moveClip(1, 42);
    expect(calls[0]).toEqual(["correction.add", { kind: "offset", clip_id: 1, other_clip_id: 2, offset_s: 42 }]);
    await useApp.getState().moveClip(2, 5);
    expect(calls.filter(([m]) => m === "correction.add")).toHaveLength(1);
    expect(useApp.getState().toasts.at(-1)?.text).toMatch(/reference clip/);
  });

  it("applies a confident snap and reports how far it moved", async () => {
    const calls = installBridge({
      "sync.snap": () => ({ offset_s: 10.5, confidence: 0.97, status: "confident", flags: [], alternatives: [] }),
      "correction.add": () => timeline(10.5),
      "sync.matches": () => [],
    });
    await useApp.getState().snap(1);
    expect(calls[0]).toEqual(["sync.snap", { clip_id: 1, anchor_clip_id: 2, approx_offset_s: 10, radius_s: 2 }]);
    expect(useApp.getState().timeline!.groups[0]!.clips[0]!.start_s).toBe(10.5);
    expect(useApp.getState().toasts.at(-1)?.text).toContain("+500.0 ms");
  });

  it("does not move a clip on an uncertain snap", async () => {
    const calls = installBridge({
      "sync.snap": () => ({
        offset_s: 11,
        confidence: 0.4,
        status: "uncertain",
        flags: ["ambiguous"],
        alternatives: [],
      }),
    });
    await useApp.getState().snap(1);
    expect(calls.map(([m]) => m)).toEqual(["sync.snap"]);
    expect(useApp.getState().toasts.at(-1)?.kind).toBe("info");
  });

  it("shows engine errors as toasts", async () => {
    installBridge({});
    await useApp.getState().undo();
    expect(useApp.getState().toasts.at(-1)).toMatchObject({ kind: "error", text: "no correction.undo" });
  });
});
