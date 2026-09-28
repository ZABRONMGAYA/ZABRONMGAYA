// S10 Search: moments by what was said, who said it, markers and clip names. ↵ opens the moment, ⌘↵ marks it.
import { Search, SearchX } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { SearchHit } from "../../api/contract";
import { Button, EmptyState, Eyebrow, shortcut } from "../../design-system/components";
import { formatDuration } from "../../lib/format";
import { speakerName, useAnalyze } from "../../state/analyze";
import { useProd } from "../../state/production";
import { useThumbs } from "../../state/thumbs";
import { clipClock, highlight } from "./TranscriptView";

const MATCHED: Record<string, string> = {
  speech: "Speech",
  speaker: "Speaker",
  marker: "Marker",
  "clip name": "Clip name",
};

const LOW = 0.85; // results below this are shown amber: AI search is a lead, not a fact

export function SearchView() {
  const query = useAnalyze((s) => s.query);
  const results = useAnalyze((s) => s.results);
  const searching = useAnalyze((s) => s.searching);
  const activeHit = useAnalyze((s) => s.activeHit);
  const speakers = useAnalyze((s) => s.speakers);
  const markers = useAnalyze((s) => s.markers);
  const overview = useAnalyze((s) => s.overview);
  const rows = useProd((s) => s.rows);
  const [text, setText] = useState(query);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const byId = useMemo(() => new Map(rows.map((r) => [r.clip_id, r])), [rows]);
  const hits = results ?? [];
  const active = hits[activeHit];

  useEffect(() => setText(query), [query]);

  // Real suggestions only: named speakers and marker labels of this project.
  const suggestions = useMemo(() => {
    const out: string[] = [];
    for (const s of speakers) if (s.name) out.push(s.name);
    for (const m of markers) if (m.label && !out.includes(m.label)) out.push(m.label);
    return out.slice(0, 5);
  }, [speakers, markers]);

  const run = (value: string, now = false) => {
    setText(value);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void useAnalyze.getState().search(value), now ? 0 : 250);
  };

  const open = (hit: SearchHit) => void useAnalyze.getState().openClip(hit.clip_id, Math.max(0, hit.t_s - 0.5));
  const mark = (hit: SearchHit) =>
    void useAnalyze.getState().addMarker(hit.clip_id, hit.t_s, `“${query.trim()}”`, "search");

  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-hit="${activeHit}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [activeHit]);

  const want = useThumbs((s) => s.want);
  const thumbs = useThumbs((s) => s.urls);
  useEffect(() => {
    if (hits.length) want([...new Set(hits.slice(0, 60).map((h) => h.clip_id))]);
  }, [hits, want]);

  const covered = overview?.totals.speech_s ?? 0;

  return (
    <div className="sy-search-screen" data-testid="search-view">
      <div className="sy-search-screen__query">
        <div className="sy-search-xl">
          <Search size={16} aria-hidden />
          <input
            autoFocus
            value={text}
            placeholder="Search what was said, speakers and markers"
            aria-label="Search moments"
            data-testid="moment-search"
            onChange={(e) => run(e.target.value)}
            onKeyDown={(e) => {
              const a = useAnalyze.getState();
              if (e.key === "ArrowDown") {
                e.preventDefault();
                a.setActiveHit(Math.min(hits.length - 1, activeHit + 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                a.setActiveHit(Math.max(0, activeHit - 1));
              } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                if (active) mark(active);
              } else if (e.key === "Enter") {
                e.preventDefault();
                if (active && text === query) open(active);
                else run(text, true);
              } else if (e.key === "Escape") {
                run("", true);
              }
            }}
          />
          <span className="sy-search-xl__hint">↵ open · {shortcut("⌘↵")} add marker</span>
        </div>
        <div className="sy-search-screen__try">
          {suggestions.length > 0 && (
            <>
              <span>Try:</span>
              {suggestions.map((s) => (
                <button key={s} type="button" className="sy-search-chip" onClick={() => run(s, true)}>
                  {s}
                </button>
              ))}
            </>
          )}
          <span className="sy-spacer" />
          <span>Searches transcripts, speakers, markers and clip names · {formatDuration(covered)} of speech</span>
        </div>
      </div>

      <div className="sy-search-screen__body">
        <div className="sy-search-screen__list" ref={listRef}>
          {!query.trim() ? (
            <EmptyState
              icon={Search}
              title="Search the production"
              body="Type a phrase someone said, a speaker’s name or a marker. Results are sorted by how well they match."
            />
          ) : hits.length === 0 && !searching ? (
            <EmptyState
              icon={SearchX}
              title={`No results for “${query}”`}
              body={`Try fewer words, or search by speaker. Transcripts cover ${formatDuration(covered)} of speech in ${overview?.totals.clips ?? 0} recording${overview?.totals.clips === 1 ? "" : "s"}.`}
              action={
                <Button variant="secondary" size="compact" onClick={() => run("", true)}>
                  Clear search
                </Button>
              }
            />
          ) : (
            <>
              <Eyebrow className="sy-search-screen__count">
                {searching
                  ? "Searching…"
                  : `${hits.length} moment${hits.length === 1 ? "" : "s"} · sorted by confidence`}
              </Eyebrow>
              {hits.map((h, k) => {
                const row = byId.get(h.clip_id);
                const low = h.score < LOW;
                return (
                  <div
                    key={`${h.clip_id}:${h.t_s}:${h.kind}`}
                    data-hit={k}
                    className={`sy-hit ${k === activeHit ? "sy-hit--active" : ""}`}
                    role="button"
                    tabIndex={-1}
                    onClick={() => useAnalyze.getState().setActiveHit(k)}
                    onDoubleClick={() => open(h)}
                    data-testid="search-hit"
                  >
                    <div className="sy-hit__thumb">
                      {thumbs.get(h.clip_id) && <img src={thumbs.get(h.clip_id)} alt="" draggable={false} />}
                      <span className="sy-hit__cam">{(h.device_name ?? row?.device_name ?? "").toUpperCase()}</span>
                      <span className="sy-hit__tc tnum">{clipClock(row, h.t_s)}</span>
                    </div>
                    <div className="sy-hit__body">
                      <div className="sy-hit__meta">
                        <span className="sy-hit__time tnum">{clipClock(row, h.t_s)}</span>
                        {h.speaker && (
                          <span className="sy-hit__speaker">
                            {speakerName(h.speaker, h.speaker_name).toUpperCase()}
                          </span>
                        )}
                        <span className="sy-muted sy-ellipsis">{h.clip_name}</span>
                      </div>
                      <div className="sy-hit__text">{highlight(h.text, query)}</div>
                      <div className="sy-hit__chips">
                        {h.matched_by.map((m) => (
                          <span key={m} className="sy-hit__chip">
                            {MATCHED[m] ?? m}
                          </span>
                        ))}
                        <button type="button" className="sy-link" onClick={() => open(h)}>
                          Open
                        </button>
                        <button type="button" className="sy-link" onClick={() => mark(h)}>
                          Add marker
                        </button>
                      </div>
                    </div>
                    <div className="sy-hit__conf">
                      <span className="sy-muted">Confidence</span>
                      <span className={`tnum sy-hit__pct ${low ? "sy-warn" : ""}`}>{Math.round(h.score * 100)}%</span>
                    </div>
                  </div>
                );
              })}
            </>
          )}
        </div>
        <aside className="sy-search-screen__aside">
          <Eyebrow>Matched because</Eyebrow>
          {active ? (
            <dl className="sy-kv sy-search-screen__why">
              <dt>Speaker</dt>
              <dd>
                {active.speaker
                  ? `${speakerName(active.speaker, active.speaker_name)}${active.matched_by.includes("speaker") ? " (by name)" : ""}`
                  : "—"}
              </dd>
              <dt>Speech</dt>
              <dd>
                {active.kind === "speech"
                  ? active.score >= 1
                    ? "the whole phrase was said"
                    : active.score >= 0.9
                      ? "every word was said, not in this order"
                      : "some of the words were said"
                  : "—"}
              </dd>
              <dt>Marker</dt>
              <dd>{active.kind === "marker" ? active.text : "—"}</dd>
              <dt>Clip</dt>
              <dd>{active.kind === "clip" ? "its name matches" : active.clip_name}</dd>
              <dt>Language</dt>
              <dd>{active.language ? active.language.toUpperCase() : "—"}</dd>
            </dl>
          ) : (
            <p className="sy-muted">Choose a result to see why it matched.</p>
          )}
          <p className="sy-muted sy-search-screen__note">
            Results below 85% are marked amber — AI search is a lead, not a fact.
          </p>
        </aside>
      </div>
    </div>
  );
}
