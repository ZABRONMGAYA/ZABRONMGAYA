// Device letters, card metadata and status words shared by the media and sync screens.
import type { Device, MediaRow, Session } from "../../api/contract";
import type { SyncStatus } from "../../design-system/components";
import { rateLabel } from "../../lib/format";

/** Cameras get letters A, B, C… (then AA, AB…); sound recorders R1, R2…; in device order. */
export function deviceLetters(devices: Device[]): Map<number, { letter: string; audio: boolean }> {
  const out = new Map<number, { letter: string; audio: boolean }>();
  let cam = 0;
  let rec = 0;
  for (const d of devices) {
    if (d.kind === "recorder") {
      rec += 1;
      out.set(d.id, { letter: `R${rec}`, audio: true });
    } else {
      out.set(d.id, { letter: columnName(cam), audio: false });
      cam += 1;
    }
  }
  return out;
}

function columnName(n: number): string {
  let s = "";
  let k = n + 1;
  while (k > 0) {
    const r = (k - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    k = Math.floor((k - 1) / 26);
  }
  return s;
}

export function resolutionLabel(row: Pick<MediaRow, "width" | "height">): string | null {
  if (!row.width || !row.height) return null;
  if (row.width >= 3800) return "4K";
  if (row.width >= 2500) return "2.7K";
  if (row.height >= 1080) return "HD";
  if (row.height >= 720) return "720p";
  return `${row.width}×${row.height}`;
}

export function cardMeta(row: MediaRow, sessions: Map<number, Session>): string {
  const parts: string[] = [];
  if (row.kind === "video") {
    const res = resolutionLabel(row);
    if (res) parts.push(res);
    if (row.fps) parts.push(`${rateLabel(row.fps)}p`);
  } else if (row.sample_rate) {
    parts.push(`${Math.round(row.sample_rate / 100) / 10} kHz`);
  }
  parts.push(row.channels ? `${row.channels}ch` : "no audio");
  const session = row.session_id !== null ? sessions.get(row.session_id) : undefined;
  if (session) parts.push(session.label);
  return parts.join(" · ");
}

/** Clip → badge status. The confidence number goes with every badge except reference, pending and skipped. */
export function rowStatus(row: MediaRow, threshold: number): SyncStatus {
  if (row.method === "reference") return "reference";
  switch (row.category) {
    case "high_confidence":
      return "high";
    case "synchronized":
      return row.method === "manual" ? "good" : (row.confidence ?? 0) >= threshold ? "good" : "review";
    case "review":
      return "review";
    case "failed":
      return "failed";
    case "skipped":
      return "skipped";
    case "pending":
      return row.analysis === "running" || row.probe === "running" ? "running" : "none";
    case "manual":
      return "none";
  }
}

export function squareFor(row: MediaRow): "ok" | "review" | "failed" | "none" {
  switch (row.category) {
    case "high_confidence":
    case "synchronized":
      return "ok";
    case "review":
      return "review";
    case "failed":
      return "failed";
    default:
      return "none";
  }
}

export const CATEGORY_LABEL: Record<MediaRow["category"], string> = {
  synchronized: "Synchronized",
  high_confidence: "High confidence",
  review: "Review recommended",
  manual: "Manual sync required",
  failed: "Failed",
  skipped: "Skipped",
  pending: "Not synced yet",
};

export function formatBytes(bytes: number): string {
  if (bytes >= 2 ** 40) return `${(bytes / 2 ** 40).toFixed(2)} TB`;
  if (bytes >= 2 ** 30) return `${(bytes / 2 ** 30).toFixed(1)} GB`;
  if (bytes >= 2 ** 20) return `${(bytes / 2 ** 20).toFixed(1)} MB`;
  return `${Math.round(bytes / 1024)} KB`;
}
