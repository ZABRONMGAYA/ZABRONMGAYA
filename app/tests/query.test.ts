import { describe, expect, it } from "vitest";

import type { MediaRow } from "../src/api/contract";
import { matches, parseQuery, rowsFromIndex } from "../src/lib/query";

function row(over: Partial<MediaRow>): MediaRow {
  return {
    clip_id: 1,
    name: "A_0012.MOV",
    kind: "video",
    device_id: 1,
    device_name: "Camera A · R5",
    device_kind: "camera",
    session_id: null,
    duration_s: 42,
    fps: "25/1",
    width: 3840,
    height: 2160,
    codec: "h264",
    sample_rate: 48000,
    channels: 2,
    timecode: null,
    creation_time: new Date(2026, 5, 14, 9, 15).toISOString(),
    size_bytes: 5 * 2 ** 20,
    media_status: "online",
    duplicate_of: null,
    duplicate_decision: null,
    probe: "done",
    analysis: "done",
    sync_status: "synced",
    confidence: 0.97,
    method: "audio",
    start_s: 1,
    group: 0,
    category: "high_confidence",
    path: "/Volumes/SHOOT/A/A_0012.MOV",
    ...over,
  };
}

describe("search grammar", () => {
  it("parses the examples from the brief", () => {
    expect(parseQuery("Camera A")).toMatchObject([{ kind: "text", text: "camera a" }]);
    expect(parseQuery("08:30 - 10:00")).toEqual([{ kind: "time", from: 510, to: 600 }]);
    expect(parseQuery("Unsynchronized")).toEqual([{ kind: "status", status: "unsynchronized" }]);
    expect(parseQuery("Confidence < 80%")).toEqual([{ kind: "compare", field: "confidence", op: "<", value: 0.8 }]);
  });

  it("combines clauses and keeps text phrases between keywords", () => {
    const clauses = parseQuery("Camera A review 08:30-10:00 duration > 1 min");
    expect(clauses.find((c) => c.kind === "text")).toMatchObject({ kind: "text", text: "camera a" });
    expect(clauses).toContainEqual({ kind: "status", status: "review" });
    expect(clauses).toContainEqual({ kind: "compare", field: "duration", op: ">", value: 60 });
    expect(parseQuery("not synced")).toEqual([{ kind: "status", status: "unsynchronized" }]);
  });

  it("matches rows", () => {
    const a = row({});
    expect(matches(a, parseQuery("camera a"))).toBe(true);
    expect(matches(a, parseQuery("Camera B"))).toBe(false);
    // Several words: the phrase, or every word in the name, camera or folder.
    expect(matches(a, parseQuery("SHOOT A_0012"))).toBe(true);
    expect(matches(a, parseQuery("SHOOT A_0013"))).toBe(false);
    const other = row({ name: "C0001.MP4", device_name: "Camera (S00_CAMA)", path: "/Volumes/SHOOT/CAMA/C0001.MP4" });
    expect(matches(other, parseQuery("Camera A"))).toBe(false);
    expect(matches(a, parseQuery("08:30 - 10:00"))).toBe(true);
    expect(matches(a, parseQuery("10:00 - 11:00"))).toBe(false);
    expect(matches(a, parseQuery("confidence < 80%"))).toBe(false);
    expect(matches(row({ confidence: 0.72, category: "review" }), parseQuery("Confidence < 80% review"))).toBe(true);
    expect(matches(row({ category: "manual", confidence: null }), parseQuery("unsynchronized"))).toBe(true);
    expect(matches(a, parseQuery("fps = 25"))).toBe(true);
    expect(matches(row({ media_status: "offline" }), parseQuery("offline"))).toBe(true);
    expect(matches(row({ kind: "audio", fps: null }), parseQuery("audio"))).toBe(true);
  });

  it("reads the engine's columnar index", () => {
    const rows = rowsFromIndex(["clip_id", "name"], [[7, "x.wav"]]);
    expect(rows[0]).toEqual({ clip_id: 7, name: "x.wav" });
  });
});
