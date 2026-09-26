// Time and number formatting for the timeline and inspector.

/** "30000/1001" → 29.97…; null for audio-only clips. */
export function parseRate(rate: string | null | undefined): number | null {
  if (!rate) return null;
  const [num, den = "1"] = rate.split("/");
  const value = Number(num) / Number(den);
  return Number.isFinite(value) && value > 0 ? value : null;
}

/** Display name of a frame rate: 23.976, 25, 29.97… */
export function rateLabel(rate: string | null | undefined): string {
  const value = parseRate(rate);
  if (value === null) return "audio";
  const rounded = Math.round(value);
  return Math.abs(value - rounded) < 1e-6 ? String(rounded) : value.toFixed(3).replace(/0+$/, "");
}

/** Seconds → "H:MM:SS.mmm" (or "MM:SS.mmm" under an hour). Negative values keep their sign. */
export function formatTime(seconds: number | null | undefined, digits = 3): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const sign = seconds < 0 ? "-" : "";
  const scale = 10 ** digits;
  let total = Math.round(Math.abs(seconds) * scale);
  const frac = total % scale;
  total = Math.floor(total / scale);
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  const fraction = digits > 0 ? `.${String(frac).padStart(digits, "0")}` : "";
  const mmss = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}${fraction}`;
  return h > 0 ? `${sign}${h}:${mmss}` : `${sign}${mmss}`;
}

/** Seconds → timecode-style "HH:MM:SS:FF" at a frame rate (non-drop labels, display only). */
export function formatTimecode(seconds: number, rate: number): string {
  const fps = Math.round(rate);
  const frames = Math.floor(Math.max(0, seconds) * rate + 1e-6);
  const ff = frames % fps;
  const total = Math.floor(frames / fps);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}:${pad(ff)}`;
}

/** Short duration: "2 h 05 min", "3 min 20 s", "12.5 s". */
export function formatDuration(seconds: number): string {
  if (seconds >= 3600) {
    const h = Math.floor(seconds / 3600);
    return `${h} h ${String(Math.round((seconds - h * 3600) / 60)).padStart(2, "0")} min`;
  }
  if (seconds >= 60) {
    const m = Math.floor(seconds / 60);
    return `${m} min ${String(Math.round(seconds - m * 60)).padStart(2, "0")} s`;
  }
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
}

/** Signed milliseconds with a unit that stays readable from µs to seconds. */
export function formatOffset(seconds: number): string {
  const abs = Math.abs(seconds);
  const sign = seconds < 0 ? "−" : "+";
  if (abs < 0.001) return `${sign}${(abs * 1e6).toFixed(0)} µs`;
  if (abs < 1) return `${sign}${(abs * 1e3).toFixed(1)} ms`;
  return `${sign}${abs.toFixed(3)} s`;
}

/** A "nice" ruler step (s) so ticks are at least `minPx` apart at `pxPerSec`. */
export function rulerStep(pxPerSec: number, minPx = 80): number {
  const steps = [0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
  for (const step of steps) if (step * pxPerSec >= minPx) return step;
  return steps[steps.length - 1]!;
}
