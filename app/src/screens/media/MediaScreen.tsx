// S03 Media import and S04 Media browser, built for thousands of clips: windowed grid and list, bins by camera,
// session and status, the search grammar (lib/query.ts), multi-selection and bulk actions.
import {
  AudioLines,
  ChevronDown,
  CopyCheck,
  File,
  Folder,
  Import,
  LayoutGrid,
  List,
  Play,
  RefreshCw,
  Search,
  Unlink,
  Video,
  X,
} from "lucide-react";
import { type KeyboardEvent, type MouseEvent, useEffect, useMemo, useRef } from "react";

import type { MediaRow, Session } from "../../api/contract";
import { bridge, call } from "../../api/client";
import { Button, KeyHint, StatusSquare, SyncBadge, shortcut } from "../../design-system/components";
import { formatDuration, formatTime } from "../../lib/format";
import { gridLayout, scrollIntoView, useViewport, visibleRange } from "../../lib/virtual";
import { type Bin, useProd, visibleRows } from "../../state/production";
import { useApp } from "../../state/store";
import { useThumbs } from "../../state/thumbs";
import { CATEGORY_LABEL, cardMeta, deviceLetters, formatBytes, rowStatus, squareFor } from "./labels";

const LIST_ROW = 36;
const CARD_MIN = 200;
const CARD_GAP = 16;
const CARD_BODY = 48;

export function MediaScreen() {
  const rows = useProd((s) => s.rows);
  const devices = useProd((s) => s.devices);
  const sessions = useProd((s) => s.sessions);
  const bin = useProd((s) => s.bin);
  const query = useProd((s) => s.query);
  const importing = useProd((s) => s.importing);
  const view = useProd((s) => s.mediaView);
  const shown = useMemo(() => visibleRows({ rows, bin, query }), [rows, bin, query]);
  const letters = useMemo(() => deviceLetters(devices), [devices]);
  const sessionMap = useMemo(() => new Map(sessions.map((s) => [s.id, s])), [sessions]);
  const threshold = useApp((s) => s.project?.settings.review_threshold ?? 0.85);

  return (
    <div className="sy-media" data-testid="media-screen">
      <Bins />
      <section className="sy-media__main">
        <Banners />
        <Toolbar />
        <Summary shown={shown} />
        {importing && <ImportPanel />}
        {rows.length === 0 && !importing ? (
          <EmptyMedia />
        ) : view === "grid" ? (
          <MediaGrid rows={shown} letters={letters} sessions={sessionMap} threshold={threshold} />
        ) : (
          <MediaList rows={shown} letters={letters} sessions={sessionMap} threshold={threshold} />
        )}
        <SelectionBar shown={shown} />
      </section>
    </div>
  );
}

// ------------------------------------------------------------------- bins

function Bins() {
  const rows = useProd((s) => s.rows);
  const devices = useProd((s) => s.devices);
  const sessions = useProd((s) => s.sessions);
  const bin = useProd((s) => s.bin);
  const letters = useMemo(() => deviceLetters(devices), [devices]);
  const counts = useMemo(() => {
    const c = { review: 0, unmatched: 0, offline: 0, duplicates: 0, failed: 0 };
    for (const r of rows) {
      if (r.category === "review") c.review++;
      if (r.session_id === null && r.category !== "pending") c.unmatched++;
      if (r.media_status !== "online") c.offline++;
      if (r.duplicate_of !== null) c.duplicates++;
      if (r.category === "failed") c.failed++;
    }
    return c;
  }, [rows]);

  const item = (id: Bin, label: string, count: number, chip?: { text: string; tone?: "warn" | "audio" }) => (
    <button
      key={id}
      type="button"
      className="sy-bin"
      aria-current={bin === id ? "true" : undefined}
      onClick={(e) => {
        useProd.getState().setBin(id);
        if (e.detail === 2) selectBin(id); // double-click selects everything in the bin
      }}
      title={`${label}: ${count.toLocaleString()} clips (double-click to select them all)`}
      data-testid={`bin-${id}`}
    >
      <span
        className={`sy-bin__chip ${chip ? "" : "sy-bin__chip--none"} ${chip?.tone ? `sy-bin__chip--${chip.tone}` : ""}`}
      >
        {chip?.text ?? ""}
      </span>
      <span className="sy-bin__name">{label}</span>
      <span className="sy-bin__count">{count.toLocaleString()}</span>
    </button>
  );

  return (
    <aside className="sy-bins" aria-label="Bins">
      {item("all", "All media", rows.length)}
      <div className="sy-bins__group">Sources</div>
      {devices.map((d) => {
        const l = letters.get(d.id);
        return item(`device:${d.id}`, d.name, d.clips, { text: l?.letter ?? "", tone: l?.audio ? "audio" : undefined });
      })}
      {sessions.length > 0 && <div className="sy-bins__group">Sessions</div>}
      {sessions.map((s, k) => item(`session:${s.id}`, sessionLabel(s), s.clips, { text: String(k + 1) }))}
      <div className="sy-bins__group">Status</div>
      {item("unmatched", "Unmatched", counts.unmatched)}
      {item("review", "Needs review", counts.review, { text: "!", tone: "warn" })}
      {item("failed", "Failed", counts.failed)}
      {item("offline", "Offline", counts.offline)}
      {item("duplicates", "Duplicates", counts.duplicates)}
    </aside>
  );
}

export function sessionLabel(s: Session): string {
  if (!s.start_at) return s.label;
  const d = new Date(s.start_at);
  const when = d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  return `${s.label} · ${when}`;
}

function selectBin(bin: Bin): void {
  const state = useProd.getState();
  const ids = visibleRows({ rows: state.rows, bin, query: "" }).map((r) => r.clip_id);
  state.selectMany(ids);
}

// ---------------------------------------------------------------- banners

function Banners() {
  const offline = useProd((s) => s.offline);
  const rows = useProd((s) => s.rows);
  const undecided = useMemo(
    () => rows.filter((r) => r.duplicate_of !== null && r.duplicate_decision === null).length,
    [rows],
  );
  const volumes = offline?.volumes.filter((v) => !v.ignored) ?? [];
  return (
    <>
      {volumes.map((v) => (
        <div key={v.volume} className="sy-banner sy-media__banner" role="alert" data-testid="offline-banner">
          <Unlink size={20} className="sy-banner__icon" aria-hidden />
          <div>
            <div className="sy-banner__title">
              {v.count} clip{v.count === 1 ? "" : "s"} offline · {v.volume}{" "}
              {v.online ? "(files moved or changed)" : "unavailable"}
            </div>
            <div className="sy-banner__body">
              Reconnect the drive or point Syncora to the new location. Analysis already done is kept; clips stay in
              place.
            </div>
          </div>
          <div className="sy-banner__actions">
            <Button variant="warning" size="compact" onClick={() => void relinkFolder()}>
              Find folder…
            </Button>
            <Button
              variant="secondary"
              size="compact"
              onClick={() => void relinkOne(v.media[0]!.media_id, v.media[0]!.filename)}
            >
              Relink media…
            </Button>
            <Button
              variant="ghost"
              size="compact"
              onClick={() =>
                void useApp.getState().run(async () => {
                  useProd.setState({ offline: await call("media.ignore_offline", { volume: v.volume }) });
                })
              }
            >
              Ignore
            </Button>
          </div>
        </div>
      ))}
      {undecided > 0 && (
        <div className="sy-banner sy-media__banner sy-banner--neutral" data-testid="duplicates-banner">
          <CopyCheck size={20} className="sy-banner__icon" aria-hidden />
          <div>
            <div className="sy-banner__title">
              {undecided} possible duplicate{undecided === 1 ? "" : "s"} left out of sync
            </div>
            <div className="sy-banner__body">Copies of files already in the project. Nothing is deleted.</div>
          </div>
          <div className="sy-banner__actions">
            <Button variant="secondary" size="compact" onClick={() => useProd.getState().openDialog("duplicates")}>
              Review duplicates…
            </Button>
          </div>
        </div>
      )}
    </>
  );
}

async function relinkFolder(): Promise<void> {
  const folder = await bridge().chooseFolder("Find offline media in a folder");
  if (!folder) return;
  await useApp.getState().run(async () => {
    const result = await call("media.relink_folder", { folder });
    useProd.setState({ offline: result });
    const app = useApp.getState();
    if (result.relinked) app.toast("success", `Relinked ${result.relinked} clip(s).`);
    else app.toast("info", "No offline media found in that folder (same name, size and content).");
    if (result.not_matching.length)
      app.toast("error", `${result.not_matching.length} file(s) have the right name but other content.`);
    await useProd.getState().loadIndex();
  });
}

async function relinkOne(mediaId: number, name: string): Promise<void> {
  const file = await bridge().chooseFile(`Locate ${name}`);
  if (!file) return;
  await useApp.getState().run(async () => {
    useProd.setState({ offline: await call("media.relink_file", { media_id: mediaId, path: file }) });
    await useProd.getState().loadIndex();
    useApp.getState().toast("success", `Relinked ${name}.`);
  });
}

// ---------------------------------------------------------------- toolbar

const QUICK_FILTERS: { label: string; query: string }[] = [
  { label: "Unsynchronized", query: "unsynchronized" },
  { label: "Review", query: "review" },
  { label: "Confidence < 80%", query: "confidence < 80%" },
  { label: "Video", query: "video" },
  { label: "Audio", query: "audio" },
  { label: "Offline", query: "offline" },
];

function Toolbar() {
  const view = useProd((s) => s.mediaView);
  const query = useProd((s) => s.query);
  const importing = useProd((s) => s.importing);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="sy-media__toolbar">
      <div className="sy-segmented" role="group" aria-label="View">
        <button
          type="button"
          aria-pressed={view === "grid"}
          onClick={() => useProd.getState().setMediaView("grid")}
          title="Grid"
        >
          <LayoutGrid size={14} aria-hidden /> Grid
        </button>
        <button
          type="button"
          aria-pressed={view === "list"}
          onClick={() => useProd.getState().setMediaView("list")}
          title="List"
        >
          <List size={14} aria-hidden /> List
        </button>
      </div>
      <span className="sy-divider" aria-hidden />
      <div className="sy-media__chips">
        {QUICK_FILTERS.map((f) => (
          <button
            key={f.query}
            type="button"
            className="sy-filter-chip"
            aria-pressed={query.trim().toLowerCase() === f.query}
            onClick={() => useProd.getState().setQuery(query.trim().toLowerCase() === f.query ? "" : f.query)}
          >
            {f.label}
            <ChevronDown size={12} aria-hidden />
          </button>
        ))}
      </div>
      <label
        className="sy-search sy-media__search"
        title="Search: names, cameras, 08:30 - 10:00, unsynchronized, confidence < 80%"
      >
        <Search size={14} aria-hidden />
        <input
          ref={input}
          value={query}
          placeholder="Filter: Camera A · 08:30 - 10:00 · confidence < 80%"
          onChange={(e) => useProd.getState().setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") useProd.getState().setQuery("");
          }}
          data-testid="media-search"
          aria-label="Search media"
        />
        {query && (
          <button
            type="button"
            className="sy-search__clear"
            aria-label="Clear search"
            onClick={() => useProd.getState().setQuery("")}
          >
            <X size={12} aria-hidden />
          </button>
        )}
      </label>
      <Button
        variant={importing ? "tertiary" : "secondary"}
        size="compact"
        icon={Import}
        onClick={() => useProd.getState().setImporting(!importing)}
        data-testid="toggle-import"
      >
        Import
      </Button>
      <button
        type="button"
        className="sy-btn sy-btn--icon"
        title="Rescan imported folders for new, changed and missing files"
        aria-label="Rescan folders"
        onClick={() =>
          void useApp.getState().run(async () => {
            await call("media.rescan", {});
            useApp.getState().toast("info", "Rescanning imported folders…");
          })
        }
      >
        <RefreshCw size={14} aria-hidden />
      </button>
    </div>
  );
}

function Summary({ shown }: { shown: MediaRow[] }) {
  const all = useProd((s) => s.rows);
  const devices = useProd((s) => s.devices);
  const query = useProd((s) => s.query);
  const bin = useProd((s) => s.bin);
  const cameras = devices.filter((d) => d.kind !== "recorder").length;
  const audio = devices.filter((d) => d.kind === "recorder").length;
  const total = useMemo(() => shown.reduce((t, r) => t + (r.duration_s ?? 0), 0), [shown]);
  const bytes = useMemo(() => shown.reduce((t, r) => t + r.size_bytes, 0), [shown]);
  return (
    <div className="sy-media__summary" data-testid="media-summary">
      <span>
        <strong>{shown.length.toLocaleString()} clips</strong>
        {shown.length !== all.length && <span className="sy-muted"> of {all.length.toLocaleString()}</span>} · {cameras}{" "}
        camera{cameras === 1 ? "" : "s"} · {audio} audio source{audio === 1 ? "" : "s"} · {formatDuration(total)} ·{" "}
        {formatBytes(bytes)}
      </span>
      <span className="sy-media__filters">
        {bin !== "all" && <span>Bin: {bin.replace(":", " ")}</span>}
        {query && <span>Search: “{query}”</span>}
      </span>
    </div>
  );
}

// ------------------------------------------------------------------ views

interface ViewProps {
  rows: MediaRow[];
  letters: Map<number, { letter: string; audio: boolean }>;
  sessions: Map<number, Session>;
  threshold: number;
}

function useSelectionHandlers(rows: MediaRow[]) {
  const ordered = useMemo(() => rows.map((r) => r.clip_id), [rows]);
  const click = (e: MouseEvent, id: number) => {
    const mode = e.shiftKey ? "range" : e.metaKey || e.ctrlKey ? "toggle" : "replace";
    useProd.getState().select(id, mode, ordered);
  };
  return { ordered, click };
}

function openInTimeline(row: MediaRow): void {
  useProd.getState().setStage("timeline");
  useApp.getState().select(row.clip_id);
  useApp.getState().reveal(row.clip_id);
}

function keyNav(e: KeyboardEvent, rows: MediaRow[], columns: number, onMove: (index: number) => void): void {
  const state = useProd.getState();
  const current = state.anchor === null ? -1 : rows.findIndex((r) => r.clip_id === state.anchor);
  const step: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: columns, ArrowUp: -columns };
  if (e.key in step) {
    e.preventDefault();
    const next = Math.max(0, Math.min(rows.length - 1, current < 0 ? 0 : current + step[e.key]!));
    const row = rows[next];
    if (!row) return;
    state.select(
      row.clip_id,
      e.shiftKey ? "range" : "replace",
      rows.map((r) => r.clip_id),
    );
    onMove(next);
  } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "a") {
    e.preventDefault();
    state.selectMany(rows.map((r) => r.clip_id));
  } else if (e.key === "Escape") {
    state.clearSelection();
  } else if (e.key === "Enter" && current >= 0) {
    openInTimeline(rows[current]!);
  } else if ((e.metaKey || e.ctrlKey) && e.key === "Backspace" && state.selection.size) {
    e.preventDefault();
    state.openDialog("remove");
  }
}

function MediaGrid({ rows, letters, sessions, threshold }: ViewProps) {
  const vp = useViewport();
  const selection = useProd((s) => s.selection);
  const { click } = useSelectionHandlers(rows);
  const { columns, cardWidth, rowHeight } = gridLayout(vp.width, CARD_MIN, CARD_GAP, CARD_GAP, CARD_BODY);
  const lines = Math.ceil(rows.length / columns);
  const { first, last, total } = visibleRange(lines, rowHeight, vp.top, vp.height, 3);
  const items = rows.slice(first * columns, last * columns);
  const thumbs = useThumbs((s) => s.urls);
  useEffect(() => {
    const ids = items.filter((r) => r.kind === "video" && r.media_status === "online").map((r) => r.clip_id);
    if (ids.length) useThumbs.getState().want(ids);
  }, [items]);
  return (
    <div
      className="sy-media__scroll"
      ref={vp.ref}
      onScroll={vp.onScroll}
      tabIndex={0}
      role="grid"
      aria-label="Media"
      aria-rowcount={lines}
      aria-multiselectable
      onKeyDown={(e) =>
        keyNav(e, rows, columns, (i) => scrollIntoView(vp.ref.current, Math.floor(i / columns), rowHeight))
      }
      data-testid="media-grid"
    >
      <div style={{ height: total + CARD_GAP, position: "relative" }}>
        {items.map((row, k) => {
          const index = first * columns + k;
          const l = row.device_id !== null ? letters.get(row.device_id) : undefined;
          return (
            <div
              key={row.clip_id}
              role="gridcell"
              aria-selected={selection.has(row.clip_id)}
              className="sy-card"
              style={{
                position: "absolute",
                top: CARD_GAP + Math.floor(index / columns) * rowHeight,
                left: CARD_GAP + (index % columns) * (cardWidth + CARD_GAP),
                width: cardWidth,
              }}
              onClick={(e) => click(e, row.clip_id)}
              onDoubleClick={() => openInTimeline(row)}
              title={row.path}
              data-testid={`bin-clip-${row.name}`}
              data-status={row.category}
            >
              <div className={`sy-card__thumb sy-card__thumb--${(row.device_id ?? 0) % 5}`}>
                {thumbs.has(row.clip_id) ? (
                  <img className="sy-card__img" src={thumbs.get(row.clip_id)} alt="" draggable={false} />
                ) : row.kind === "audio" ? (
                  <AudioLines size={24} aria-hidden />
                ) : (
                  <Video size={24} aria-hidden />
                )}
                {l && (
                  <span
                    className={`sy-chip-letter ${l.audio ? "sy-chip-letter--audio" : "sy-chip-letter--dark"} sy-card__cam`}
                  >
                    {l.letter}
                  </span>
                )}
                <span className="sy-card__status">
                  <StatusSquare status={squareFor(row)} />
                </span>
                <span className="sy-card__dur">{formatTime(row.duration_s, 0)}</span>
                {row.media_status !== "online" && <span className="sy-card__offline">OFFLINE</span>}
              </div>
              <div className="sy-card__body">
                <div className="sy-card__name">{row.name}</div>
                <div className="sy-card__meta">
                  {cardMeta(row, sessions)}
                  {row.category !== "pending" && row.category !== "skipped" && row.confidence !== null && (
                    <> · {Math.round(row.confidence * 100)}%</>
                  )}
                </div>
              </div>
              <span className="sy-visually-hidden">{rowStatus(row, threshold)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

const LIST_COLUMNS = "44px minmax(160px, 1.6fr) 44px 80px 70px 60px 110px minmax(150px, 1fr) 190px";

// DESIGN-OPEN: #10 the list view is not drawn: it reuses the S08 results table rows (DESIGN_DECISIONS.md D-05).
function MediaList({ rows, letters, sessions, threshold }: ViewProps) {
  const vp = useViewport();
  const selection = useProd((s) => s.selection);
  const { click } = useSelectionHandlers(rows);
  const { first, last, total } = visibleRange(rows.length, LIST_ROW, vp.top, vp.height);
  return (
    <div className="sy-media__listwrap">
      <div className="sy-table-head" style={{ gridTemplateColumns: LIST_COLUMNS }}>
        <span />
        <span>File</span>
        <span>Cam</span>
        <span>Duration</span>
        <span>Format</span>
        <span>Ch</span>
        <span>Recorded</span>
        <span>Session · status</span>
        <span>Sync</span>
      </div>
      <div
        className="sy-media__scroll"
        ref={vp.ref}
        onScroll={vp.onScroll}
        tabIndex={0}
        role="grid"
        aria-label="Media"
        aria-rowcount={rows.length}
        aria-multiselectable
        onKeyDown={(e) => keyNav(e, rows, 1, (i) => scrollIntoView(vp.ref.current, i, LIST_ROW))}
        data-testid="media-list"
      >
        <div style={{ height: total, position: "relative" }}>
          {rows.slice(first, last).map((row, k) => {
            const l = row.device_id !== null ? letters.get(row.device_id) : undefined;
            const status = rowStatus(row, threshold);
            const session = row.session_id !== null ? sessions.get(row.session_id) : undefined;
            return (
              <div
                key={row.clip_id}
                role="row"
                aria-selected={selection.has(row.clip_id)}
                className="sy-table-row sy-media__row"
                style={{
                  gridTemplateColumns: LIST_COLUMNS,
                  position: "absolute",
                  top: (first + k) * LIST_ROW,
                  left: 0,
                  right: 0,
                }}
                onClick={(e) => click(e, row.clip_id)}
                onDoubleClick={() => openInTimeline(row)}
                title={row.path}
                data-testid={`bin-clip-${row.name}`}
                data-status={row.category}
              >
                <StatusSquare status={squareFor(row)} />
                <span className="sy-media__file">{row.name}</span>
                <span>
                  {l && <span className={`sy-chip-letter ${l.audio ? "sy-chip-letter--audio" : ""}`}>{l.letter}</span>}
                </span>
                <span className="tnum">{formatTime(row.duration_s, 0)}</span>
                <span>{row.kind === "video" ? (row.codec ?? "video") : (row.codec ?? "audio")}</span>
                <span className="tnum">{row.channels ?? "—"}</span>
                <span className="tnum sy-muted">
                  {row.creation_time
                    ? new Date(row.creation_time).toLocaleString(undefined, {
                        day: "2-digit",
                        month: "short",
                        hour: "2-digit",
                        minute: "2-digit",
                      })
                    : "—"}
                </span>
                <span className="sy-muted sy-ellipsis">
                  {session ? session.label : "Unmatched"} · {CATEGORY_LABEL[row.category]}
                </span>
                <span>
                  <SyncBadge
                    status={status}
                    confidence={["high", "good", "review"].includes(status) ? row.confidence : undefined}
                  />
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function EmptyMedia() {
  return (
    <div className="sy-media__empty">
      <div className="sy-empty">
        <Import size={28} className="sy-empty__icon" aria-hidden />
        <div className="sy-empty__title">No media</div>
        <div className="sy-empty__body">Drag cards or folders here, or browse. Original files are never modified.</div>
        <div>
          <Button variant="secondary" size="compact" onClick={() => useProd.getState().setImporting(true)}>
            Import media
          </Button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ import

function ImportPanel() {
  const pipeline = useProd((s) => s.pipeline);
  const d = pipeline?.discovery;
  const probe = pipeline?.stages.probe;
  const analyze = pipeline?.stages.analyze;
  const read = (probe?.done ?? 0) + (probe?.failed ?? 0) + (probe?.skipped ?? 0);
  const analysed = (analyze?.done ?? 0) + (analyze?.failed ?? 0) + (analyze?.skipped ?? 0);
  const analyseTotal = analysed + (analyze?.pending ?? 0) + (analyze?.running ?? 0);
  const walking = d?.walking ?? false;
  return (
    <div className="sy-import" data-testid="import-panel">
      <div
        className="sy-dropzone"
        onClick={() => void useApp.getState().importMedia("folder")}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && void useApp.getState().importMedia("folder")}
      >
        <Import size={28} aria-hidden />
        <div>
          <div className="sy-dropzone__title">Drop cards, folders or files</div>
          <div className="sy-dim">
            Syncora reads every file in place and never modifies originals. Thousands of files are fine: work continues
            while they are processed.
          </div>
        </div>
        <div className="sy-dropzone__formats">MOV · MP4 · MXF · MTS · WAV · BWF · MP3 · FLAC</div>
      </div>
      <div className="sy-import__actions">
        <Button
          variant="secondary"
          size="compact"
          icon={File}
          onClick={() => void useApp.getState().importMedia("files")}
          data-testid="import-files"
        >
          Browse files…
        </Button>
        <Button
          variant="secondary"
          size="compact"
          icon={Folder}
          onClick={() => void useApp.getState().importMedia("folder")}
          data-testid="import-folder"
        >
          Browse folders…
        </Button>
        <span className="sy-import__progress" data-testid="import-progress">
          <strong>
            {pipeline?.state === "paused" ? "Paused · " : ""}
            {walking ? "Finding files" : "Found"} {(d?.total ?? 0).toLocaleString()} files
          </strong>
          <span className="sy-dim tnum">
            {" "}
            · metadata {read.toLocaleString()} / {(d?.total ?? 0).toLocaleString()} · audio {analysed.toLocaleString()}{" "}
            / {analyseTotal.toLocaleString()}
            {(probe?.failed ?? 0) + (analyze?.failed ?? 0) > 0 && (
              <span className="sy-warn"> · {(probe?.failed ?? 0) + (analyze?.failed ?? 0)} failed</span>
            )}
          </span>
        </span>
        {pipeline?.state === "paused" && (
          <Button
            variant="secondary"
            size="compact"
            icon={Play}
            onClick={() => void useProd.getState().resumePipeline()}
            data-testid="import-resume"
          >
            Resume
          </Button>
        )}
        <Button
          variant="tertiary"
          size="compact"
          onClick={() => {
            useProd.getState().setStage("sync");
            useProd.getState().setSyncView("analysis");
          }}
        >
          Show progress
        </Button>
        <button
          type="button"
          className="sy-btn sy-btn--icon"
          aria-label="Hide import"
          onClick={() => useProd.getState().setImporting(false)}
        >
          <X size={14} aria-hidden />
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- selection

function SelectionBar({ shown }: { shown: MediaRow[] }) {
  const selection = useProd((s) => s.selection);
  if (selection.size === 0) return null;
  const ids = [...selection];
  const prod = useProd.getState();
  const selectedRows = shown.filter((r) => selection.has(r.clip_id));
  const groups = new Set(selectedRows.map((r) => r.group).filter((g) => g !== null));
  return (
    <div className="sy-selbar" role="toolbar" aria-label="Selected clips" data-testid="selection-bar">
      <strong className="tnum">{selection.size.toLocaleString()} selected</strong>
      <Button
        variant="primary"
        size="compact"
        onClick={() => void prod.startSync(ids)}
        title="Synchronise, these clips first"
      >
        Sync
      </Button>
      <Button variant="secondary" size="compact" onClick={() => void prod.analyze(ids)} data-testid="bulk-analyze">
        Analyze
      </Button>
      <Button
        variant="secondary"
        size="compact"
        disabled
        title="Transcription is not included in this version of Syncora"
      >
        Transcribe
      </Button>
      <Button
        variant="secondary"
        size="compact"
        disabled={groups.size !== 1}
        title={
          groups.size === 1
            ? "Export the sync group of these clips"
            : "Select clips from one synchronised session to export it"
        }
        onClick={() => {
          const [g] = [...groups];
          if (g !== undefined && g !== null) useApp.getState().setGroup(g);
          useApp.getState().openExport();
        }}
      >
        Export
      </Button>
      <Button variant="secondary" size="compact" onClick={() => void prod.retry(ids)}>
        Retry
      </Button>
      <Button
        variant="secondary"
        size="compact"
        onClick={() => void prod.prioritize(ids)}
        title="Process these clips before the rest"
      >
        Prioritize
      </Button>
      <Button variant="secondary" size="compact" onClick={() => prod.openDialog("assign-camera")}>
        Assign camera…
      </Button>
      <Button variant="secondary" size="compact" onClick={() => prod.openDialog("assign-session")}>
        Assign group…
      </Button>
      <Button
        variant="destructive"
        size="compact"
        onClick={() => prod.openDialog("remove")}
        shortcut={shortcut("⌘⌫")}
        data-testid="bulk-remove"
      >
        Remove from project
      </Button>
      <span className="sy-selbar__spacer" />
      <Button variant="ghost" size="compact" onClick={() => prod.clearSelection()}>
        Clear <KeyHint keys="Esc" />
      </Button>
    </div>
  );
}
