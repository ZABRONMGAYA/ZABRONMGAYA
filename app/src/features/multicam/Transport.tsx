// Transport of the Sync workspace's timeline bar (S06): previous / play / next and the master timecode. Previous
// and next jump between the selected clip's sync points and edges (or the clip starts of the group).
import { Pause, Play, SkipBack, SkipForward } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { formatTimecode, parseRate } from "../../lib/format";
import { usePlayback } from "../../state/playback";
import { displayedGroup, findClip, useApp } from "../../state/store";
import { clock, followClock } from "./clock";
import { audioMonitor } from "./MulticamViewer";

/** Where "previous" and "next" jump: the selected clip's sync points and edges, else every clip start. */
function marks(): number[] {
  const app = useApp.getState();
  const clip = findClip(app.timeline, app.selected);
  const points = usePlayback.getState().points;
  const out: number[] = [];
  if (clip?.start_s !== null && clip?.start_s !== undefined) {
    out.push(clip.start_s, clip.start_s + clip.duration_s);
    if (points?.clipId === clip.clip_id) out.push(...points.points.map((p) => p.group_s));
  } else {
    for (const c of displayedGroup(app)?.clips ?? []) if (c.start_s !== null) out.push(c.start_s);
  }
  return [...new Set(out)].sort((a, b) => a - b);
}

export function jump(direction: -1 | 1): void {
  const t = clock.now();
  const list = marks();
  const target = direction < 0 ? [...list].reverse().find((m) => m < t - 0.02) : list.find((m) => m > t + 0.02);
  clock.pause();
  clock.seek(target ?? (direction < 0 ? 0 : clock.end));
}

function rate(): number {
  const app = useApp.getState();
  const g = displayedGroup(app);
  const ref =
    g?.clips.find((c) => c.clip_id === app.timeline?.reference_clip_id && c.has_video) ??
    g?.clips.find((c) => c.has_video);
  return parseRate(ref?.frame_rate ?? null) ?? 25;
}

export function Transport() {
  const [playing, setPlaying] = useState(clock.playing);
  const [speed, setSpeed] = useState(clock.rate);
  const tc = useRef<HTMLSpanElement>(null);
  useEffect(
    () =>
      clock.subscribe(() => {
        setPlaying(clock.playing);
        setSpeed(clock.rate);
      }),
    [],
  );
  useEffect(
    () =>
      followClock((t) => {
        if (tc.current) tc.current.textContent = formatTimecode(t, rate());
      }),
    [],
  );
  return (
    <div className="sy-transport" data-testid="transport">
      <button
        type="button"
        onClick={() => jump(-1)}
        title="Previous sync point or clip edge"
        aria-label="Previous sync point"
      >
        <SkipBack size={16} aria-hidden />
      </button>
      <button
        type="button"
        onClick={() => {
          audioMonitor();
          clock.toggle();
        }}
        title="Play / pause (Space)"
        aria-label={playing ? "Pause" : "Play"}
        data-testid="play"
      >
        {playing ? <Pause size={16} aria-hidden /> : <Play size={16} aria-hidden />}
      </button>
      <button type="button" onClick={() => jump(1)} title="Next sync point or clip edge" aria-label="Next sync point">
        <SkipForward size={16} aria-hidden />
      </button>
      <span className="sy-transport__tc tnum" ref={tc} data-testid="master-timecode">
        00:00:00:00
      </span>
      {speed !== 1 && <span className="sy-transport__rate">{speed}×</span>}
    </div>
  );
}
