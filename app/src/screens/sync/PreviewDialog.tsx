// "Preview side by side" (S07): the clip and the recording it was matched with, played together at the offset AI
// sync proposes. In sync, the two sounds merge into one; a few frames off, they echo.
import { Pause, Play, RotateCcw, ScanEye } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { bridge } from "../../api/client";
import type { AiCandidate, MediaRow } from "../../api/contract";
import { Button, Dialog, Segmented } from "../../design-system/components";
import { formatTime } from "../../lib/format";

type Sound = "both" | "clip" | "anchor";

export function PreviewDialog({
  clip,
  anchor,
  candidate,
  onClose,
}: {
  clip: MediaRow;
  anchor: MediaRow;
  candidate: AiCandidate;
  onClose: () => void;
}) {
  const a = useRef<HTMLVideoElement>(null);
  const b = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [sound, setSound] = useState<Sound>("both");
  const [t, setT] = useState(0);
  const [failed, setFailed] = useState<string | null>(null);
  const offset = candidate.offset_s; // clip time + offset = anchor time
  const start = momentOf(candidate, clip.duration_s ?? 0, anchor.duration_s ?? 0);

  const place = (clipT: number) => {
    if (a.current) a.current.currentTime = Math.max(0, clipT);
    if (b.current) b.current.currentTime = Math.max(0, clipT + offset);
    setT(clipT);
  };

  useEffect(() => {
    place(start);
  }, [start]); // place() reads refs only

  useEffect(() => {
    if (a.current) a.current.muted = sound === "anchor";
    if (b.current) b.current.muted = sound === "clip";
  }, [sound]);

  // Keep the anchor on the clip while playing (the two decoders drift apart by a frame or two otherwise).
  useEffect(() => {
    if (!playing) return;
    const id = setInterval(() => {
      const va = a.current;
      const vb = b.current;
      if (!va || !vb) return;
      setT(va.currentTime);
      const want = va.currentTime + offset;
      if (Math.abs(vb.currentTime - want) > 0.04 && want >= 0) vb.currentTime = want;
    }, 250);
    return () => clearInterval(id);
  }, [playing, offset]);

  const toggle = async () => {
    const va = a.current;
    const vb = b.current;
    if (!va || !vb) return;
    if (playing) {
      va.pause();
      vb.pause();
      setPlaying(false);
      return;
    }
    place(va.currentTime);
    try {
      await Promise.all([va.play(), vb.play()]);
      setPlaying(true);
    } catch (err) {
      setFailed(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Dialog
      title="Preview side by side"
      icon={ScanEye}
      iconTone="info"
      wide
      onClose={onClose}
      testId="ai-preview"
      footer={
        <>
          <Segmented
            label="Sound"
            value={sound}
            onChange={setSound}
            options={[
              { value: "both", label: "Both" },
              { value: "clip", label: "This clip" },
              { value: "anchor", label: "Matched" },
            ]}
          />
          <span className="sy-spacer" />
          <Button variant="secondary" size="36" icon={RotateCcw} onClick={() => place(start)}>
            Back to the moment
          </Button>
          <Button variant="primary" size="36" icon={playing ? Pause : Play} onClick={() => void toggle()}>
            {playing ? "Pause" : "Play"}
          </Button>
        </>
      }
    >
      <p className="sy-dim">
        Both play from the moment the evidence points to. In sync, voices and claps sound once; off by a few frames,
        they echo.
      </p>
      <div className="sy-preview">
        {[
          { ref: a, row: clip, time: t },
          { ref: b, row: anchor, time: t + offset },
        ].map(({ ref, row, time }) => (
          <figure key={row.clip_id} className="sy-preview__tile">
            <video
              ref={ref}
              src={bridge().mediaUrl(row.path)}
              preload="auto"
              playsInline
              onError={() => setFailed(`${row.name} cannot be played here (its format is not supported for preview).`)}
            />
            <figcaption>
              <span className="sy-chip-letter sy-chip-letter--dark">{row.device_name ?? row.name}</span>
              <span className="tnum">
                {row.name} · {formatTime(time)}
              </span>
            </figcaption>
          </figure>
        ))}
      </div>
      {failed && <p className="sy-warn">{failed}</p>}
    </Dialog>
  );
}

/** Where to start the preview: at the first strong evidence, else in the middle of the overlap (clip time, s). */
function momentOf(c: AiCandidate, clipDur: number, anchorDur: number): number {
  for (const lane of ["speech", "visual", "audio"] as const) {
    const cells = c.lanes[lane];
    const i = cells?.findIndex((v) => v >= 2) ?? -1;
    if (i >= 0) return Math.max(0, ((i + 0.5) / 48) * clipDur - 2);
  }
  const lo = Math.max(0, -c.offset_s);
  const hi = Math.min(clipDur, anchorDur - c.offset_s);
  return hi > lo ? lo + (hi - lo) / 2 : 0;
}
