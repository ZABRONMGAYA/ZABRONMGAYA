import { useMemo, useState } from "react";

import { call } from "../../api/client";
import type { ClipSummary, Device, DeviceKind } from "../../api/contract";
import { formatDuration, rateLabel } from "../../lib/format";
import { findClip, useApp, usePick } from "../../state/store";

const KINDS: DeviceKind[] = ["camera", "recorder", "phone", "drone", "other"];

function DeviceHeader({ device }: { device: Device }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(device.name);
  const run = useApp((s) => s.run);

  async function save(changes: { name?: string; kind?: DeviceKind }) {
    setEditing(false);
    await run(async () => {
      const media = await call("device.update", { device_id: device.id, ...changes });
      useApp.setState({ media });
    });
  }

  return (
    <div className="device-header">
      {editing ? (
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => void save({ name })}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save({ name });
            if (e.key === "Escape") setEditing(false);
          }}
        />
      ) : (
        <span className="device-name" onDoubleClick={() => setEditing(true)} title="Double-click to rename">
          {device.name}
        </span>
      )}
      <select
        className="kind"
        value={device.kind}
        onChange={(e) => void save({ kind: e.target.value as DeviceKind })}
        title="Device type"
      >
        {KINDS.map((k) => (
          <option key={k} value={k}>
            {k}
          </option>
        ))}
      </select>
    </div>
  );
}

function AudioChoice({ clip }: { clip: ClipSummary }) {
  const run = useApp((s) => s.run);
  const stream = clip.audio_streams.find((a) => a.index === clip.audio_stream);
  const options: { value: string; label: string }[] = [{ value: "none", label: "no audio" }];
  for (const a of clip.audio_streams) {
    options.push({ value: `${a.index}:`, label: `stream ${a.index} (${a.channels} ch mix)` });
    if (a.channels > 1) {
      for (let ch = 0; ch < a.channels; ch++)
        options.push({ value: `${a.index}:${ch}`, label: `stream ${a.index} ch ${ch + 1}` });
    }
  }
  if (options.length <= 2 && (stream?.channels ?? 1) <= 1) return null;
  const value = clip.audio_stream === null ? "none" : `${clip.audio_stream}:${clip.audio_channel ?? ""}`;

  async function change(v: string) {
    const [s, ch] = v === "none" ? [null, null] : v.split(":");
    await run(async () => {
      await call("clip.set_audio", {
        clip_id: clip.clip_id,
        stream_index: s === null || s === undefined ? null : Number(s),
        channel: ch ? Number(ch) : null,
      });
      useApp.getState().invalidatePeaks();
      await useApp.getState().refresh();
    });
  }

  return (
    <select
      className="audio-choice"
      value={value}
      onChange={(e) => void change(e.target.value)}
      title="Audio used for syncing"
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function MediaBin() {
  const { media, timeline, selected, select, reveal } = usePick("media", "timeline", "selected", "select", "reveal");
  const byDevice = useMemo(() => {
    const groups = new Map<number | null, ClipSummary[]>();
    for (const clip of media.clips) {
      const list = groups.get(clip.device_id) ?? [];
      list.push(clip);
      groups.set(clip.device_id, list);
    }
    return groups;
  }, [media.clips]);

  if (media.clips.length === 0) {
    return (
      <nav className="bin empty" data-testid="media-bin">
        <p className="muted">Import or drop a folder of camera cards and recorder files to begin.</p>
      </nav>
    );
  }

  return (
    <nav className="bin" data-testid="media-bin">
      {media.devices.map((device) => (
        <section key={device.id} className="device">
          <DeviceHeader device={device} />
          <ul>
            {(byDevice.get(device.id) ?? []).map((clip) => {
              const placed = findClip(timeline, clip.clip_id);
              const status = placed?.status ?? "unsynced";
              return (
                <li
                  key={clip.clip_id}
                  className={`clip-row ${selected === clip.clip_id ? "selected" : ""}`}
                  onClick={() => {
                    select(clip.clip_id);
                    reveal(clip.clip_id);
                  }}
                  data-testid={`bin-clip-${clip.name}`}
                >
                  <span className={`status-dot ${status}`} title={status} />
                  <span className="clip-name" title={clip.path}>
                    {clip.name}
                    {clip.chapter && <span className="badge">ch {clip.chapter.index + 1}</span>}
                    {clip.status !== "online" && <span className="badge warn">{clip.status}</span>}
                    {clip.vfr && <span className="badge warn">VFR</span>}
                  </span>
                  <span className="clip-meta">
                    {formatDuration(clip.duration_s)} · {rateLabel(clip.frame_rate)}
                    {clip.timecode ? ` · TC ${clip.timecode}` : ""}
                  </span>
                  <AudioChoice clip={clip} />
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </nav>
  );
}
