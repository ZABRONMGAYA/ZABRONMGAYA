// Store behaviour against a fake engine bridge.
import { beforeEach, describe, expect, it } from "vitest";

import type { Bridge, EngineMethods, Method, Timeline } from "../src/api/contract";
import { useApp } from "../src/state/store";
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
    platform: "test",
  };
  window.mcsync = bridge;
  return calls;
}

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

beforeEach(() => {
  useApp.setState({ timeline: timeline(10), group: 0, selected: 1, toasts: [], matches: {}, snaps: {} });
});

describe("corrections", () => {
  it("adds up rapid nudges before the engine answers", async () => {
    let start = 10;
    const calls = installBridge({
      "correction.add": async (p) => {
        await delay(20);
        start = (p as { offset_s: number }).offset_s;
        return timeline(start);
      },
      "sync.matches": () => [],
    });
    const { nudge } = useApp.getState();
    await Promise.all([nudge(1, 0.04), nudge(1, 0.04), nudge(1, 0.04)]);
    const offsets = calls.filter(([m]) => m === "correction.add").map(([, p]) => (p as { offset_s: number }).offset_s);
    expect(offsets.map((o) => o.toFixed(3))).toEqual(["10.040", "10.080", "10.120"]);
    expect(useApp.getState().timeline!.groups[0]!.clips[0]!.start_s).toBeCloseTo(10.12, 9);
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
