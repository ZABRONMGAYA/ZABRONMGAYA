// Search and filter grammar for large projects. A query is a list of clauses that must all match:
//   free text        Camera A · C0012 · ceremony      (name, folder, camera, codec)
//   time of day      08:30 - 10:00 · 14:00-15:30       (recording time, local clock)
//   status words     unsynchronized · synced · review · failed · offline · duplicate · pending · skipped
//   kinds            video · audio · no audio
//   comparisons      confidence < 80% · duration > 60s · fps = 25
import type { MediaRow } from "../api/contract";

type CompareOp = "<" | "<=" | ">" | ">=" | "=";

export type Clause =
  | { kind: "text"; text: string; words?: RegExp[] }
  | { kind: "time"; from: number; to: number }
  | { kind: "status"; status: StatusWord }
  | { kind: "compare"; field: "confidence" | "duration" | "fps" | "size"; op: CompareOp; value: number };

type StatusWord =
  | "unsynchronized"
  | "synchronized"
  | "review"
  | "failed"
  | "offline"
  | "duplicate"
  | "pending"
  | "skipped"
  | "video"
  | "audio"
  | "noaudio"
  | "manual";

const STATUS_WORDS: Record<string, StatusWord> = {
  unsynchronized: "unsynchronized",
  unsynchronised: "unsynchronized",
  unsynced: "unsynchronized",
  "not synced": "unsynchronized",
  "not-synced": "unsynchronized",
  synchronized: "synchronized",
  synchronised: "synchronized",
  synced: "synchronized",
  review: "review",
  "needs review": "review",
  failed: "failed",
  errors: "failed",
  offline: "offline",
  missing: "offline",
  duplicate: "duplicate",
  duplicates: "duplicate",
  pending: "pending",
  skipped: "skipped",
  video: "video",
  videos: "video",
  audio: "audio",
  "no audio": "noaudio",
  manual: "manual",
  "manual sync": "manual",
};

const TIME = String.raw`(\d{1,2}):(\d{2})`;
const TIME_RANGE = new RegExp(String.raw`${TIME}\s*(?:-|–|to)\s*${TIME}`, "gi");
const COMPARE =
  /\b(confidence|conf|duration|length|fps|size)\s*(<=|>=|<|>|=)\s*(\d+(?:\.\d+)?)\s*(%|s|sec|m|min|mb|gb)?/gi;

export function parseQuery(query: string): Clause[] {
  const clauses: Clause[] = [];
  let rest = ` ${query} `;
  rest = rest.replace(TIME_RANGE, (_m, h1: string, m1: string, h2: string, m2: string) => {
    clauses.push({ kind: "time", from: Number(h1) * 60 + Number(m1), to: Number(h2) * 60 + Number(m2) });
    return " ";
  });
  rest = rest.replace(COMPARE, (_m, name: string, op: string, raw: string, unit: string | undefined) => {
    const lower = name.toLowerCase();
    let value = Number(raw);
    let field: "confidence" | "duration" | "fps" | "size";
    if (lower.startsWith("conf")) {
      field = "confidence";
      value = unit === "%" || value > 1 ? value / 100 : value;
    } else if (lower === "duration" || lower === "length") {
      field = "duration";
      if (unit && unit.startsWith("m")) value *= 60;
    } else if (lower === "size") {
      field = "size";
      value *= unit?.toLowerCase() === "gb" ? 2 ** 30 : 2 ** 20;
    } else {
      field = "fps";
    }
    clauses.push({ kind: "compare", field, op: op as CompareOp, value });
    return " ";
  });
  // Multi-word status phrases first, then single words; what is left is free text (kept as phrases between
  // recognised words, so "Camera A" stays one clause).
  for (const phrase of Object.keys(STATUS_WORDS).filter((w) => w.includes(" "))) {
    const re = new RegExp(`\\b${phrase.replace(" ", "\\s+")}\\b`, "gi");
    rest = rest.replace(re, () => {
      clauses.push({ kind: "status", status: STATUS_WORDS[phrase]! });
      return " | ";
    });
  }
  const words = rest.split(/\s+/);
  let text: string[] = [];
  const flush = () => {
    if (text.length) {
      const phrase = text.join(" ").toLowerCase();
      clauses.push(
        text.length > 1 ? { kind: "text", text: phrase, words: text.map(wordPattern) } : { kind: "text", text: phrase },
      );
    }
    text = [];
  };
  for (const word of words) {
    if (!word) continue;
    if (word === "|") {
      flush();
      continue;
    }
    const status = STATUS_WORDS[word.toLowerCase()];
    if (status) {
      flush();
      clauses.push({ kind: "status", status });
    } else {
      text.push(word);
    }
  }
  flush();
  return clauses;
}

/** A word of a multi-word search: found at the start of a word; single letters only as whole words (so
 * "Camera A" does not match every name containing an "a"). */
function wordPattern(word: string): RegExp {
  const escaped = word.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(word.length === 1 ? `(^|[^a-z0-9])${escaped}($|[^a-z0-9])` : `(^|[^a-z0-9])${escaped}`);
}

function minutesOfDay(iso: string | null): number | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.getHours() * 60 + d.getMinutes();
}

function compare(a: number, op: string, b: number): boolean {
  switch (op) {
    case "<":
      return a < b;
    case "<=":
      return a <= b + 1e-9;
    case ">":
      return a > b;
    case ">=":
      return a >= b - 1e-9;
    default:
      return Math.abs(a - b) < 1e-6;
  }
}

function fpsValue(fps: string | null): number | null {
  if (!fps) return null;
  const [n, d] = fps.split("/").map(Number);
  return d ? n! / d : (n ?? null);
}

export function matchesStatus(row: MediaRow, status: StatusWord): boolean {
  switch (status) {
    case "unsynchronized":
      return row.category === "manual" || row.category === "pending" || row.category === "failed";
    case "synchronized":
      return row.category === "synchronized" || row.category === "high_confidence";
    case "review":
      return row.category === "review";
    case "failed":
      return row.category === "failed" || row.analysis === "failed" || row.probe === "failed";
    case "offline":
      return row.media_status !== "online";
    case "duplicate":
      return row.duplicate_of !== null;
    case "pending":
      return row.category === "pending";
    case "skipped":
      return row.category === "skipped";
    case "manual":
      return row.category === "manual";
    case "video":
      return row.kind === "video";
    case "audio":
      return row.kind === "audio";
    case "noaudio":
      return row.channels === null;
  }
}

export function matches(row: MediaRow, clauses: Clause[]): boolean {
  for (const c of clauses) {
    if (c.kind === "text") {
      const hay = `${row.name} ${row.device_name ?? ""} ${row.path} ${row.codec ?? ""}`.toLowerCase();
      // The words as a phrase ("Camera A"), or each word somewhere ("S01_CAMD C0030").
      if (!hay.includes(c.text) && !(c.words && c.words.every((w) => w.test(hay)))) return false;
    } else if (c.kind === "time") {
      const m = minutesOfDay(row.creation_time);
      if (m === null) return false;
      const inside = c.from <= c.to ? m >= c.from && m <= c.to : m >= c.from || m <= c.to;
      if (!inside) return false;
    } else if (c.kind === "status") {
      if (!matchesStatus(row, c.status)) return false;
    } else {
      const value =
        c.field === "confidence"
          ? row.confidence
          : c.field === "duration"
            ? row.duration_s
            : c.field === "size"
              ? row.size_bytes
              : fpsValue(row.fps);
      if (value === null || !compare(value, c.op, c.value)) return false;
    }
  }
  return true;
}

/** Turn the engine's columnar index into rows. */
export function rowsFromIndex(columns: string[], values: unknown[][]): MediaRow[] {
  return values.map((v) => {
    const row: Record<string, unknown> = {};
    for (let k = 0; k < columns.length; k++) row[columns[k]!] = v[k];
    return row as unknown as MediaRow;
  });
}
