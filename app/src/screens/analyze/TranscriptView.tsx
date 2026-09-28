// S09 Transcript · speakers · language: the clip's video with its captions, what was said (by whom, in which
// language), and the speakers of the whole project (SYNCORA_TRANSCRIPT_UI_SPECIFICATION.md).
import {
  AudioLines,
  Captions,
  Check,
  ChevronDown,
  CircleX,
  Flag,
  Languages,
  Pause,
  Play,
  SkipBack,
  SkipForward,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { bridge } from "../../api/client";
import type { MediaRow, TranscriptSegment } from "../../api/contract";
import { Button, EmptyState, Select } from "../../design-system/components";
import { formatTime, formatTimecode, parseRate } from "../../lib/format";
import { speakerLabel, speakerName, useAnalyze } from "../../state/analyze";
import { useProd } from "../../state/production";
import { useApp } from "../../state/store";
import { SpeakersColumn } from "./SpeakersView";

/** Timestamp of a moment of a clip: timecode-style at its frame rate, else H:MM:SS. */
export function clipClock(row: Pick<MediaRow, "fps"> | undefined, t: number): string {
  const rate = parseRate(row?.fps ?? null);
  return rate ? formatTimecode(t, rate) : formatTime(t, 0);
}

function hhmmss(t: number): string {
  const s = Math.max(0, Math.floor(t));
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}

/** The text with every occurrence of `find` marked (case-insensitive). */
export function highlight(text: string, find: string): ReactNode {
  const q = find.trim();
  if (!q) return text;
  const out: ReactNode[] = [];
  const lower = text.toLowerCase();
  const needle = q.toLowerCase();
  let from = 0;
  for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, at + needle.length)) {
    if (at > from) out.push(text.slice(from, at));
    out.push(<mark key={at}>{text.slice(at, at + needle.length)}</mark>);
    from = at + needle.length;
  }
  out.push(text.slice(from));
  return out;
}

export function TranscriptView() {
  const clipId = useAnalyze((s) => s.clipId);
  const transcript = useAnalyze((s) => s.transcript);
  const overview = useAnalyze((s) => s.overview);
  const status = useAnalyze((s) => s.status);
  const seek = useAnalyze((s) => s.seek);
  const speakers = useAnalyze((s) => s.speakers);
  const rows = useProd((s) => s.rows);
  const [t, setT] = useState(0);
  const [find, setFind] = useState("");
  const [seekTo, setSeekTo] = useState<{ t: number; token: number } | null>(null);
  const token = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);
  const userScrolled = useRef(0);

  const byId = useMemo(() => new Map(rows.map((r) => [r.clip_id, r])), [rows]);
  const row = clipId !== null ? byId.get(clipId) : undefined;
  const segments = transcript?.segments ?? [];
  const markers = transcript?.markers ?? [];
  const names = useMemo(() => new Map(speakers.map((s) => [s.key, s.name])), [speakers]);
  const order = useMemo(() => new Map(speakers.map((s, k) => [s.key, k])), [speakers]);
  const clipState = overview?.clips.find((c) => c.clip_id === clipId);

  // Clips to choose from: the transcribed ones first, then every other clip with sound.
  const choices = useMemo(() => {
    const done = new Set(overview?.clips.map((c) => c.clip_id) ?? []);
    const withSound = rows.filter((r) => r.channels !== null && r.channels > 0);
    withSound.sort((a, b) => Number(done.has(b.clip_id)) - Number(done.has(a.clip_id)) || a.name.localeCompare(b.name));
    return withSound.map((r) => ({
      value: r.clip_id,
      label: `${done.has(r.clip_id) ? "● " : ""}${r.name}${r.device_name ? ` · ${r.device_name}` : ""}`,
    }));
  }, [rows, overview]);

  useEffect(() => {
    if (seek) {
      setSeekTo({ t: seek.t, token: ++token.current });
      setT(seek.t);
    }
  }, [seek]);

  useEffect(() => {
    setT(0);
    setFind("");
  }, [clipId]);

  const active = useMemo(() => {
    let k = -1;
    for (let i = 0; i < segments.length; i++) {
      if (segments[i]!.start_s <= t + 0.05) k = i;
      else break;
    }
    return k;
  }, [segments, t]);

  const jump = (target: number) => {
    setSeekTo({ t: target, token: ++token.current });
    setT(target);
  };

  // Keep the playing row in the upper third, unless the list was scrolled by hand in the last 3 s.
  useEffect(() => {
    const list = listRef.current;
    if (!list || active < 0 || Date.now() - userScrolled.current < 3000) return;
    const el = list.querySelector<HTMLElement>(`[data-row="${active}"]`);
    if (!el) return;
    const top = el.offsetTop - list.offsetTop;
    if (top < list.scrollTop || top > list.scrollTop + list.clientHeight * 0.66) {
      list.scrollTo({ top: Math.max(0, top - list.clientHeight / 3) });
    }
  }, [active]);

  // ⇧↑ / ⇧↓ previous / next segment; M adds a marker at the playhead.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping() || clipId === null) return;
      if (e.shiftKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
        e.preventDefault();
        const k = e.key === "ArrowUp" ? Math.max(0, active - 1) : Math.min(segments.length - 1, active + 1);
        if (segments[k]) jump(segments[k].start_s);
      } else if (e.key.toLowerCase() === "m" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        void useAnalyze.getState().addMarker(clipId, t);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const nextMatch = () => {
    const q = find.trim().toLowerCase();
    if (!q) return;
    const n = segments.length;
    for (let i = 1; i <= n; i++) {
      const k = (Math.max(active, -1) + i) % n;
      if (segments[k]!.text.toLowerCase().includes(q)) {
        userScrolled.current = 0;
        jump(segments[k]!.start_s);
        return;
      }
    }
  };

  if (clipId === null) {
    return (
      <div className="sy-transcript sy-transcript--empty">
        <EmptyState
          icon={Captions}
          title="No transcript"
          body={
            choices.length
              ? "Choose a recording to see what was said in it."
              : "Transcription hasn’t run for this project. It runs on device."
          }
          action={
            choices.length ? (
              <Select
                label="Recording"
                value={choices[0]!.value}
                width={320}
                options={choices}
                onChange={(id) => void useAnalyze.getState().openClip(id)}
              />
            ) : undefined
          }
        />
      </div>
    );
  }

  const running = clipState?.status === "running" || clipState?.status === "queued";
  const failed = clipState?.status === "failed";
  const shared = segments.length > 0 && segments.every((s) => s.source_clip_id !== clipId);
  const activeSegment = active >= 0 ? segments[active] : undefined;
  const caption =
    activeSegment && t <= activeSegment.end_s + 0.4
      ? `${activeSegment.speaker ? `${speakerName(activeSegment.speaker, names.get(activeSegment.speaker)).toUpperCase()}: ` : ""}${activeSegment.text}`
      : null;

  return (
    <div className="sy-transcript" data-testid="transcript-view">
      <section className="sy-transcript__video">
        <div className="sy-transcript__picker">
          <Select
            label="Recording"
            value={clipId}
            width={420}
            options={choices.length ? choices : [{ value: clipId, label: row?.name ?? `Clip ${clipId}` }]}
            onChange={(id) => void useAnalyze.getState().openClip(id)}
          />
          <Button
            variant="secondary"
            size="compact"
            icon={Flag}
            onClick={() => void useAnalyze.getState().addMarker(clipId, t)}
            title="Mark this moment (M)"
          >
            Add marker
          </Button>
        </div>
        {row ? (
          <Player
            key={row.clip_id}
            row={row}
            caption={caption}
            seekTo={seekTo}
            onTime={setT}
            onPrev={() => segments[Math.max(0, active - 1)] && jump(segments[Math.max(0, active - 1)]!.start_s)}
            onNext={() =>
              segments[Math.min(segments.length - 1, active + 1)] &&
              jump(segments[Math.min(segments.length - 1, active + 1)]!.start_s)
            }
            t={t}
          />
        ) : (
          <div className="sy-transcript__frame">
            <p className="sy-muted">Loading…</p>
          </div>
        )}
        <MiniTimeline
          duration={row?.duration_s ?? Math.max(1, ...segments.map((s) => s.end_s))}
          segments={segments}
          markers={markers}
          t={t}
          order={order}
          onSeek={jump}
        />
      </section>

      <section className="sy-transcript__list">
        <div className="sy-transcript__find">
          <input
            className="sy-input"
            placeholder="Find in transcript"
            aria-label="Find in transcript"
            value={find}
            onChange={(e) => setFind(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") nextMatch();
              if (e.key === "Escape") setFind("");
            }}
          />
          <LanguageButton segments={segments} disabled={running && segments.length === 0} />
        </div>
        {shared && (
          <div className="sy-transcript__note">
            Heard by {byId.get(segments[0]!.source_clip_id)?.name ?? "another recording"} at the same moment (this clip
            was not transcribed itself).
          </div>
        )}
        <div
          className="sy-transcript__rows"
          ref={listRef}
          onWheel={() => (userScrolled.current = Date.now())}
          data-testid="transcript-rows"
        >
          {failed && segments.length === 0 ? (
            <div className="sy-transcript__failed">
              <CircleX size={20} aria-hidden />
              <div>
                <div className="sy-banner__title">Transcription failed</div>
                <div className="sy-banner__body">
                  Some parts of {row?.name ?? "this recording"} could not be transcribed. The reason is in Sync →
                  Progress → View errors.
                </div>
                <div className="sy-transcript__failed-actions">
                  <Button
                    variant="primary"
                    size="compact"
                    onClick={() => void useAnalyze.getState().transcribe("clips", [clipId], true)}
                  >
                    Retry
                  </Button>
                  <Button
                    variant="secondary"
                    size="compact"
                    onClick={() => useProd.getState().openSettings("transcription")}
                  >
                    Choose language
                  </Button>
                </div>
              </div>
            </div>
          ) : segments.length === 0 ? (
            <EmptyState
              icon={Captions}
              title={running ? "Transcribing…" : "No transcript"}
              body={
                running
                  ? "Lines appear here as each part is transcribed."
                  : "This recording hasn’t been transcribed. It runs on device."
              }
              action={
                running ? undefined : (
                  <Button
                    variant="secondary"
                    size="compact"
                    disabled={!status?.ready}
                    onClick={() => void useAnalyze.getState().transcribe("clips", [clipId])}
                    data-testid="transcribe-clip"
                  >
                    Transcribe
                  </Button>
                )
              }
            />
          ) : (
            segments.map((s, k) => (
              <Row
                key={`${s.source_clip_id}:${s.id}`}
                index={k}
                segment={s}
                active={k === active}
                last={running && k === segments.length - 1}
                name={names.get(s.speaker ?? "")}
                find={find}
                onClick={() => jump(s.start_s)}
              />
            ))
          )}
        </div>
      </section>

      <SpeakersColumn />
    </div>
  );
}

function Row({
  index,
  segment,
  active,
  last,
  name,
  find,
  onClick,
}: {
  index: number;
  segment: TranscriptSegment;
  active: boolean;
  last: boolean;
  name: string | null | undefined;
  find: string;
  onClick: () => void;
}) {
  return (
    <div
      className={`sy-trow ${active ? "sy-trow--active" : ""}`}
      data-row={index}
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => e.key === "Enter" && onClick()}
      aria-current={active || undefined}
    >
      <span className="sy-trow__time tnum">{hhmmss(segment.start_s)}</span>
      <div>
        <div className="sy-trow__head">
          <span className="sy-trow__speaker">{speakerName(segment.speaker, name).toUpperCase()}</span>
          {segment.speaker && name && (
            <span className="sy-trow__id">{speakerLabel(segment.speaker).toUpperCase()}</span>
          )}
          <span className="sy-trow__lang">{segment.language ? segment.language.toUpperCase() : "—"}</span>
        </div>
        <div className="sy-trow__text">
          {highlight(segment.text, find)}
          {last ? "…" : ""}
        </div>
      </div>
    </div>
  );
}

function Player({
  row,
  caption,
  seekTo,
  onTime,
  onPrev,
  onNext,
  t,
}: {
  row: MediaRow;
  caption: string | null;
  seekTo: { t: number; token: number } | null;
  onTime: (t: number) => void;
  onPrev: () => void;
  onNext: () => void;
  t: number;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const audioOnly = row.kind === "audio";

  useEffect(() => {
    const v = video.current;
    if (v && seekTo) v.currentTime = seekTo.t;
  }, [seekTo]);

  const toggle = () => {
    const v = video.current;
    if (!v) return;
    if (v.paused) void v.play().catch((err: unknown) => setError(String(err)));
    else v.pause();
  };

  // Space play/pause; J back 5 s, K pause, L play (the editing convention).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping() || e.metaKey || e.ctrlKey || e.altKey) return;
      const v = video.current;
      if (!v) return;
      if (e.key === " " && !(document.activeElement instanceof HTMLButtonElement)) {
        e.preventDefault();
        toggle();
      } else if (e.key === "j") {
        v.currentTime = Math.max(0, v.currentTime - 5);
      } else if (e.key === "k") {
        v.pause();
      } else if (e.key === "l") {
        void v.play().catch(() => undefined);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <>
      <div className={`sy-transcript__frame ${audioOnly ? "sy-transcript__frame--audio" : ""}`}>
        <span className="sy-transcript__chip">
          {(row.device_name ?? "").toUpperCase()}
          {row.device_name ? " · " : ""}
          {row.name}
        </span>
        <video
          ref={video}
          src={bridge().mediaUrl(row.path)}
          preload="metadata"
          playsInline
          onTimeUpdate={(e) => onTime(e.currentTarget.currentTime)}
          onSeeked={(e) => onTime(e.currentTarget.currentTime)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onError={() =>
            setError(
              row.media_status !== "online"
                ? "The file is offline: reconnect its drive to play it."
                : "This format cannot be previewed here. The transcript, search and markers still work.",
            )
          }
          onClick={toggle}
        />
        {audioOnly && !error && <AudioLines size={48} className="sy-transcript__audio-icon" aria-hidden />}
        {error && <p className="sy-transcript__error">{error}</p>}
        {caption && <div className="sy-transcript__caption">{caption}</div>}
      </div>
      <div className="sy-transport">
        <button type="button" className="sy-btn sy-btn--icon" aria-label="Previous segment" onClick={onPrev}>
          <SkipBack size={16} aria-hidden />
        </button>
        <button
          type="button"
          className="sy-btn sy-btn--icon"
          aria-label={playing ? "Pause" : "Play"}
          onClick={toggle}
          data-testid="transcript-play"
        >
          {playing ? <Pause size={16} aria-hidden /> : <Play size={16} aria-hidden />}
        </button>
        <button type="button" className="sy-btn sy-btn--icon" aria-label="Next segment" onClick={onNext}>
          <SkipForward size={16} aria-hidden />
        </button>
        <span className="sy-transport__tc tnum">{clipClock(row, t)}</span>
        <span className="sy-transport__hint">J K L · ⇧↑↓ previous / next segment · M marker</span>
      </div>
    </>
  );
}

function MiniTimeline({
  duration,
  segments,
  markers,
  t,
  order,
  onSeek,
}: {
  duration: number;
  segments: TranscriptSegment[];
  markers: { id: number; t_s: number; label: string | null; source: string }[];
  t: number;
  order: Map<string, number>;
  onSeek: (t: number) => void;
}) {
  const d = Math.max(duration, 0.001);
  const pct = (x: number) => `${Math.max(0, Math.min(100, (x / d) * 100))}%`;
  return (
    <div
      className="sy-mini"
      onClick={(e) => {
        const box = e.currentTarget.getBoundingClientRect();
        onSeek(((e.clientX - box.left) / box.width) * d);
      }}
      role="slider"
      aria-label="Position"
      aria-valuemin={0}
      aria-valuemax={Math.round(d)}
      aria-valuenow={Math.round(t)}
      tabIndex={0}
    >
      <div className="sy-mini__lane" aria-label="Markers">
        {markers.map((m) => (
          <span
            key={m.id}
            className={`sy-marker-tile ${m.source === "user" ? "sy-marker-tile--user" : ""}`}
            style={{ left: pct(m.t_s) }}
            title={`${m.label ?? "Marker"} · ${formatTime(m.t_s, 0)}`}
          />
        ))}
      </div>
      <div className="sy-mini__lane" aria-label="Transcript">
        {segments.map((s) => (
          <span
            key={`${s.source_clip_id}:${s.id}`}
            className={`sy-mini__seg sy-mini__seg--${(order.get(s.speaker ?? "") ?? 3) % 4}`}
            style={{ left: pct(s.start_s), width: pct(Math.max(0.2, s.end_s - s.start_s)) }}
          />
        ))}
      </div>
      <div className="sy-mini__scale tnum">
        <span>{formatTime(0, 0)}</span>
        <span>{formatTime(d / 2, 0)}</span>
        <span>{formatTime(d, 0)}</span>
      </div>
      <span className="sy-mini__playhead" style={{ left: pct(t) }} />
    </div>
  );
}

function LanguageButton({ segments, disabled }: { segments: TranscriptSegment[]; disabled: boolean }) {
  const status = useAnalyze((s) => s.status);
  const clipId = useAnalyze((s) => s.clipId);
  const project = useApp((s) => s.project);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const setting = project?.settings.transcription_language ?? "auto";
  const names = useMemo(() => new Map(status?.languages.map((l) => [l.code, l.name]) ?? []), [status]);
  const mix = useMemo(() => {
    const counts = new Map<string, number>();
    for (const s of segments) if (s.language) counts.set(s.language, (counts.get(s.language) ?? 0) + 1);
    const total = [...counts.values()].reduce((a, b) => a + b, 0);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([code, n]) => ({ code, share: n / total }));
  }, [segments]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", esc);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("keydown", esc);
    };
  }, [open]);

  const label =
    setting === "auto"
      ? `Auto${
          mix.length
            ? ` · ${mix
                .slice(0, 2)
                .map((m) => m.code.toUpperCase())
                .join(" + ")}`
            : ""
        }`
      : (names.get(setting) ?? setting);
  const choose = (code: string) => {
    void useApp.getState().updateSettings({ transcription_language: code });
    setOpen(false);
  };

  return (
    <div className="sy-lang" ref={ref}>
      <button
        type="button"
        className={`sy-lang__button ${open ? "sy-lang__button--open" : ""}`}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="listbox"
        data-testid="language-button"
      >
        <Languages size={14} aria-hidden />
        {label}
        <ChevronDown size={14} aria-hidden />
      </button>
      {open && (
        <div className="sy-lang__popover" role="listbox" aria-label="Transcription language">
          <button
            type="button"
            className={`sy-lang__head ${setting === "auto" ? "sy-lang__row--selected" : ""}`}
            onClick={() => choose("auto")}
            role="option"
            aria-selected={setting === "auto"}
          >
            <span className="sy-lang__auto">Auto detect {setting === "auto" && <Check size={12} aria-hidden />}</span>
            <span className="sy-lang__detected">
              Detected:{" "}
              {mix.length
                ? mix
                    .slice(0, 3)
                    .map((m) => `${names.get(m.code) ?? m.code} ${Math.round(m.share * 100)}%`)
                    .join(" · ")
                : "unknown"}
            </span>
          </button>
          <div className="sy-lang__list">
            {(status?.languages ?? []).map((l) => (
              <button
                key={l.code}
                type="button"
                role="option"
                aria-selected={setting === l.code}
                className={`sy-lang__row ${setting === l.code ? "sy-lang__row--selected" : ""}`}
                onClick={() => choose(l.code)}
              >
                <span>
                  {l.name} {setting === l.code && <Check size={12} aria-hidden />}
                </span>
              </button>
            ))}
          </div>
          <div className="sy-lang__foot">
            Applies to the next transcription.{" "}
            {clipId !== null && (
              <button
                type="button"
                className="sy-link"
                onClick={() => {
                  setOpen(false);
                  void useAnalyze.getState().transcribe("clips", [clipId], true);
                }}
              >
                Transcribe this recording again
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function isTyping(): boolean {
  const el = document.activeElement;
  return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}
