// Project presets: starting settings for common kinds of shoot, applied when a project is created.
import type { ProjectSettings } from "../api/contract";

export interface Preset {
  id: string;
  name: string;
  description: string;
  settings: Partial<ProjectSettings>;
  /** What the preset sets, in words (shown on its card). */
  summary: string[];
}

export const PRESETS: Preset[] = [
  {
    id: "standard",
    name: "Standard",
    description: "Any production: audio first, guided by timecode and recording times.",
    settings: { mode: "hybrid", use_creation_time: true, timecode_jam_synced: false, review_threshold: 0.85 },
    summary: ["Audio + clocks", "Recording times on", "Review below 85%"],
  },
  {
    id: "wedding",
    name: "Wedding and events",
    description:
      "Recorders and cameras started at different times, far from each other. AI looks for clips audio cannot place and proposes them for review.",
    settings: {
      mode: "hybrid",
      use_creation_time: true,
      timecode_jam_synced: false,
      review_threshold: 0.85,
      ai_fallback: true,
    },
    summary: ["Audio + clocks", "AI fallback on", "Review below 85%"],
  },
  {
    id: "timecode",
    name: "Jam-synced timecode",
    description: "Studio, broadcast or multicam shoots where every device shares one timecode clock.",
    settings: { mode: "timecode", timecode_jam_synced: true, use_creation_time: false, review_threshold: 0.85 },
    summary: ["Timecode first", "Jam-synced", "Audio checks the result"],
  },
  {
    id: "interview",
    name: "Interview and podcast",
    description:
      "Few angles, a lot of talking: a stricter review threshold, and AI placement from what was said when a microphone was off.",
    settings: { mode: "hybrid", use_creation_time: true, review_threshold: 0.9, ai_fallback: true },
    summary: ["Audio + clocks", "AI fallback on", "Review below 90%"],
  },
  {
    id: "documentary",
    name: "Run-and-gun documentary",
    description:
      "Camera clocks nobody set: audio only, recording times ignored, AI fallback for clips shot far from a microphone.",
    settings: {
      mode: "audio",
      use_creation_time: false,
      timecode_jam_synced: false,
      review_threshold: 0.85,
      ai_fallback: true,
    },
    summary: ["Audio only", "Clocks ignored", "AI fallback on"],
  },
];

const KEY = "syncora.preset";

export function defaultPresetId(): string {
  try {
    const id = localStorage.getItem(KEY);
    return id && PRESETS.some((p) => p.id === id) ? id : "standard";
  } catch {
    return "standard";
  }
}

export function setDefaultPreset(id: string): void {
  try {
    localStorage.setItem(KEY, id);
  } catch {
    // this session only
  }
}

export function presetById(id: string | undefined): Preset {
  return PRESETS.find((p) => p.id === id) ?? PRESETS[0]!;
}
