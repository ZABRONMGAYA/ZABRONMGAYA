#!/usr/bin/env node
// A stand-in engine for UI benchmarks: a synthetic 3-hour project with a recorder and 300 camera clips on
// 12 devices, answered instantly, with waveform files written to MCSYNC_CACHE_DIR like the real engine's.
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";

const RATE = 8000;
const LEVELS = [64, 256, 1024, 4096, 16384, 65536];
const DEVICES = 12;
const CLIPS_PER_DEVICE = 25;
const DURATION_S = 3 * 3600;
const cacheDir = process.env.MCSYNC_CACHE_DIR ?? path.join(process.cwd(), ".fake-cache");

function writePeaks(directory, seconds) {
  fs.mkdirSync(directory, { recursive: true });
  const bins64 = Math.ceil((seconds * RATE) / 64);
  let level = new Int8Array(bins64 * 2);
  for (let b = 0; b < bins64; b++) {
    const a = Math.min(127, Math.round(35 + 45 * Math.abs(Math.sin(b / 700)) + 30 * Math.random()));
    level[2 * b] = -a;
    level[2 * b + 1] = a;
  }
  for (const spb of LEVELS) {
    if (spb > 64) {
      const factor = spb / LEVELS[LEVELS.indexOf(spb) - 1];
      const n = Math.ceil(level.length / 2 / factor);
      const next = new Int8Array(n * 2);
      for (let b = 0; b < n; b++) {
        let lo = 127;
        let hi = -127;
        for (let k = b * factor; k < Math.min((b + 1) * factor, level.length / 2); k++) {
          lo = Math.min(lo, level[2 * k]);
          hi = Math.max(hi, level[2 * k + 1]);
        }
        next[2 * b] = lo;
        next[2 * b + 1] = hi;
      }
      level = next;
    }
    fs.writeFileSync(path.join(directory, `peaks_${spb}.i8`), level);
  }
}

const recorderDir = path.join(cacheDir, "fake", "recorder");
const cameraDir = path.join(cacheDir, "fake", "camera");
writePeaks(recorderDir, DURATION_S);
writePeaks(cameraDir, 240);

const devices = [{ id: 1, key: "rec", name: "ZOOM F8n (recorder)", kind: "recorder" }];
const clips = [
  {
    clip_id: 1,
    name: "ZOOM0001.WAV",
    device_id: 1,
    device_name: devices[0].name,
    kind: "recorder",
    duration_s: DURATION_S,
    has_video: false,
    frame_rate: null,
    start_s: 0,
    method: "reference",
  },
];
for (let d = 0; d < DEVICES; d++) {
  const device = { id: d + 2, key: `cam${d}`, name: `Camera ${String.fromCharCode(65 + d)}`, kind: "camera" };
  devices.push(device);
  for (let k = 0; k < CLIPS_PER_DEVICE; k++) {
    clips.push({
      clip_id: clips.length + 1,
      name: `${String.fromCharCode(65 + d)}${String(k + 1).padStart(3, "0")}.MP4`,
      device_id: device.id,
      device_name: device.name,
      kind: "camera",
      duration_s: 60 + ((k * 7 + d * 3) % 180),
      has_video: true,
      frame_rate: "25/1",
      start_s: k * 420 + d * 13,
      method: "audio",
    });
  }
}

const tracks = [...devices.slice(1), devices[0]].map((d, index) => ({
  index,
  device_id: d.id,
  device_name: d.name,
  kind: d.kind,
  lane: 0,
}));
const trackOf = new Map(tracks.map((t) => [t.device_id, t.index]));
const timelineClips = clips.map((c) => {
  const review = c.method === "audio" && c.clip_id % 17 === 0;
  return {
    ...c,
    path: `/footage/${c.name}`,
    has_audio: true,
    timecode: null,
    media_status: "online",
    group: 0,
    track: trackOf.get(c.device_id),
    status: review ? "needs_review" : "synced",
    confidence: review ? 0.5 : 0.98,
    flags: review ? ["ambiguous"] : [],
    drift_ppm: 0,
  };
});
const timeline = {
  groups: [{ group: 0, duration_s: DURATION_S, origin_offset_s: 0, tracks, clips: timelineClips }],
  unsynced: [],
  review: timelineClips
    .filter((c) => c.status === "needs_review")
    .map((c) => ({ clip_id: c.clip_id, reason: "uncertain", flags: c.flags })),
  reference_clip_id: 1,
  stats: {
    clips: clips.length,
    synced: timelineClips.filter((c) => c.status === "synced").length,
    needs_review: 0,
    unsynced: 0,
  },
};
timeline.stats.needs_review = timeline.review.length;

const settings = { mode: "hybrid", reference_clip_id: 1, timecode_jam_synced: false, use_creation_time: true };
let projectPath = null;
const summary = () => ({
  path: projectPath,
  name: "Benchmark",
  settings,
  clips: clips.length,
  offline: 0,
  devices: devices.length,
  last_run: 1,
});

const handlers = {
  "engine.hello": () => ({
    name: "fake-engine",
    version: "bench",
    protocol: 1,
    ffmpeg: "ffmpeg version bench",
    cache_dir: cacheDir,
    workers: 1,
  }),
  "engine.shutdown": () => {
    setTimeout(() => process.exit(0), 10);
    return { ok: true };
  },
  "project.open": ({ path: p }) => ((projectPath = p), summary()),
  "project.create": ({ path: p }) => ((projectPath = p), summary()),
  "project.info": () => summary(),
  "project.close": () => ((projectPath = null), { ok: true }),
  "media.list": () => ({
    devices: devices.map((d) => ({ ...d, make: null, model: null, serial: null, color: null })),
    clips: clips.map((c) => ({
      clip_id: c.clip_id,
      name: c.name,
      path: `/footage/${c.name}`,
      status: "online",
      device_id: c.device_id,
      device_name: c.device_name,
      kind: c.kind,
      duration_s: c.duration_s,
      frame_rate: c.frame_rate,
      vfr: false,
      timecode: null,
      creation_time: null,
      audio_streams: [{ index: 1, channels: 2, codec: "aac", sample_rate: 48000 }],
      audio_stream: 1,
      audio_channel: null,
      chapter: null,
    })),
  }),
  "timeline.get": () => timeline,
  "sync.matches": () => [],
  "correction.add": () => timeline,
  "job.list": () => [],
  "waveform.info": ({ clip_id }) => {
    const clip = clips[clip_id - 1];
    return {
      directory: clip.kind === "recorder" ? recorderDir : cameraDir,
      files: Object.fromEntries(LEVELS.map((l) => [String(l), `peaks_${l}.i8`])),
      encoding: "mulaw-int8-minmax",
      rate: RATE,
      samples: Math.round(clip.duration_s * RATE),
      level_dbfs: -20,
      audio_start_s: 0,
    };
  },
};

readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const { id, method, params } = JSON.parse(line);
  const handler = handlers[method];
  const reply = handler
    ? { jsonrpc: "2.0", id, result: handler(params ?? {}) }
    : { jsonrpc: "2.0", id, error: { code: -32601, message: `fake engine: ${method}` } };
  process.stdout.write(`${JSON.stringify(reply)}\n`);
});
