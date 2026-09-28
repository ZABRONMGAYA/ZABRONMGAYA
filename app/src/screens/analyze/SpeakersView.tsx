// Speakers: the S09 column (rename inline, drag one speaker onto another to merge) and the Speakers tab.
import { Merge, Pencil, Search, Users } from "lucide-react";
import { useState } from "react";

import type { Speaker } from "../../api/contract";
import { Button, Dialog, EmptyState, Select } from "../../design-system/components";
import { formatDuration } from "../../lib/format";
import { SPEAKER_SWATCHES, speakerLabel, speakerName, useAnalyze } from "../../state/analyze";

function Swatch({ index, speaker }: { index: number; speaker: Speaker }) {
  const n = /\d+/.exec(speaker.key)?.[0] ?? String(index + 1);
  return (
    <span className="sy-swatch" style={{ background: SPEAKER_SWATCHES[index % SPEAKER_SWATCHES.length] }}>
      {n.padStart(2, "0")}
    </span>
  );
}

/** A speaker's name, edited in place: ↵ saves, Esc cancels (F2 or the pencil starts). */
function NameEditor({ speaker, onDone }: { speaker: Speaker; onDone: () => void }) {
  const [value, setValue] = useState(speaker.name ?? "");
  const save = () => {
    const name = value.trim() || null;
    if (name !== (speaker.name ?? null)) void useAnalyze.getState().renameSpeaker(speaker.key, name);
    onDone();
  };
  return (
    <input
      className="sy-speaker__input"
      value={value}
      autoFocus
      placeholder={speakerLabel(speaker.key)}
      aria-label={`Name of ${speakerLabel(speaker.key)}`}
      onChange={(e) => setValue(e.target.value)}
      onBlur={save}
      onKeyDown={(e) => {
        if (e.key === "Enter") save();
        if (e.key === "Escape") onDone();
      }}
    />
  );
}

function MergeDialog({ from, into, onClose }: { from: Speaker; into: Speaker; onClose: () => void }) {
  return (
    <Dialog
      title={`Merge ${speakerName(from.key, from.name)} into ${speakerName(into.key, into.name)}?`}
      icon={Merge}
      iconTone="info"
      onClose={onClose}
      footer={
        <>
          <span className="sy-spacer" />
          <Button variant="secondary" size="36" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="36"
            onClick={() => {
              void useAnalyze.getState().mergeSpeakers([from.key], into.key);
              onClose();
            }}
          >
            Merge
          </Button>
        </>
      }
    >
      <p>
        Every line said by {speakerName(from.key, from.name)} ({from.segments}) becomes{" "}
        {speakerName(into.key, into.name)}’s. Use this when one person was recognised as two voices.
      </p>
    </Dialog>
  );
}

export function SpeakersColumn() {
  const speakers = useAnalyze((s) => s.speakers);
  const [editing, setEditing] = useState<string | null>(null);
  const [dragged, setDragged] = useState<string | null>(null);
  const [merge, setMerge] = useState<{ from: Speaker; into: Speaker } | null>(null);
  const shown = speakers.filter((s) => s.segments > 0);
  return (
    <aside className="sy-speakers" data-testid="speakers-column">
      <div className="sy-section-head sy-eyebrow">Speakers · {shown.length} detected</div>
      <div className="sy-speakers__list">
        {shown.map((s, k) => (
          <div
            key={s.key}
            className={`sy-speaker ${editing === s.key ? "sy-speaker--editing" : ""} ${dragged && dragged !== s.key ? "sy-speaker--drop" : ""}`}
            draggable={editing !== s.key}
            onDragStart={(e) => {
              setDragged(s.key);
              e.dataTransfer.setData("text/x-speaker", s.key);
            }}
            onDragEnd={() => setDragged(null)}
            onDragOver={(e) => {
              if (dragged && dragged !== s.key) e.preventDefault();
            }}
            onDrop={(e) => {
              e.preventDefault();
              const from = speakers.find((x) => x.key === e.dataTransfer.getData("text/x-speaker"));
              setDragged(null);
              if (from && from.key !== s.key) setMerge({ from, into: s });
            }}
            onKeyDown={(e) => e.key === "F2" && setEditing(s.key)}
            tabIndex={0}
          >
            <Swatch index={k} speaker={s} />
            <div className="sy-speaker__body">
              {editing === s.key ? (
                <NameEditor speaker={s} onDone={() => setEditing(null)} />
              ) : (
                <button
                  type="button"
                  className={`sy-speaker__name ${s.name ? "" : "sy-speaker__name--unnamed"}`}
                  onClick={() => setEditing(s.key)}
                  title="Rename (F2)"
                >
                  {speakerName(s.key, s.name)}
                </button>
              )}
              <div className="sy-speaker__id">
                {speakerLabel(s.key).toUpperCase()} · {s.segments} line{s.segments === 1 ? "" : "s"}
              </div>
            </div>
            <button
              type="button"
              className="sy-btn sy-btn--icon sy-btn--compact"
              aria-label={`Rename ${speakerName(s.key, s.name)}`}
              onClick={() => setEditing(s.key)}
            >
              <Pencil size={12} aria-hidden />
            </button>
          </div>
        ))}
      </div>
      <p className="sy-speakers__note">
        Renaming updates every segment, marker and export. ↵ to save. Drag a speaker onto another to merge them.
      </p>
      {merge && <MergeDialog from={merge.from} into={merge.into} onClose={() => setMerge(null)} />}
    </aside>
  );
}

/** The Speakers tab: every voice of the project, how much each said, rename and merge. */
export function SpeakersView() {
  const speakers = useAnalyze((s) => s.speakers);
  const [editing, setEditing] = useState<string | null>(null);
  const [targets, setTargets] = useState<Record<string, string>>({});
  const [merge, setMerge] = useState<{ from: Speaker; into: Speaker } | null>(null);
  const shown = speakers.filter((s) => s.segments > 0);
  if (!shown.length) {
    return (
      <div className="sy-analyze__pane">
        <EmptyState
          icon={Users}
          title="No speakers yet"
          body="Speakers are told apart by their voices while recordings are transcribed."
          action={
            <Button variant="secondary" size="compact" onClick={() => useAnalyze.getState().setTab("overview")}>
              Transcribe
            </Button>
          }
        />
      </div>
    );
  }
  return (
    <div className="sy-analyze__pane" data-testid="speakers-view">
      <div className="sy-analyze__pane-head">
        <h1 className="sy-analyze__h1">Speakers</h1>
        <p className="sy-dim">
          {shown.length} voices across the project. Name them once: transcripts, search and exports follow.
        </p>
      </div>
      <div className="sy-speaker-table">
        <div className="sy-speaker-table__row sy-speaker-table__row--head">
          <span />
          <span>Name</span>
          <span>Lines</span>
          <span>Speaking time</span>
          <span>Merge into</span>
          <span />
        </div>
        {shown.map((s, k) => (
          <div key={s.key} className="sy-speaker-table__row">
            <Swatch index={k} speaker={s} />
            <span>
              {editing === s.key ? (
                <NameEditor speaker={s} onDone={() => setEditing(null)} />
              ) : (
                <button
                  type="button"
                  className={`sy-speaker__name ${s.name ? "" : "sy-speaker__name--unnamed"}`}
                  onClick={() => setEditing(s.key)}
                  title="Rename"
                >
                  {speakerName(s.key, s.name)} <Pencil size={12} aria-hidden />
                </button>
              )}
              <span className="sy-speaker__id">{speakerLabel(s.key).toUpperCase()}</span>
            </span>
            <span className="tnum">{s.segments.toLocaleString()}</span>
            <span className="tnum">{formatDuration(s.speech_s)}</span>
            <span className="sy-speaker-table__merge">
              <Select
                label={`Merge ${speakerName(s.key, s.name)} into`}
                value={targets[s.key] ?? ""}
                width={180}
                onChange={(v) => setTargets((t) => ({ ...t, [s.key]: v }))}
                options={[
                  { value: "", label: "—" },
                  ...shown
                    .filter((o) => o.key !== s.key)
                    .map((o) => ({ value: o.key, label: speakerName(o.key, o.name) })),
                ]}
              />
              <Button
                variant="secondary"
                size="compact"
                icon={Merge}
                disabled={!targets[s.key]}
                onClick={() => {
                  const into = shown.find((o) => o.key === targets[s.key]);
                  if (into) setMerge({ from: s, into });
                }}
              >
                Merge
              </Button>
            </span>
            <span>
              <Button
                variant="ghost"
                size="compact"
                icon={Search}
                disabled={!s.name}
                title={s.name ? `Find what ${s.name} said` : "Name the speaker to search for them"}
                onClick={() => {
                  const a = useAnalyze.getState();
                  a.setTab("search");
                  void a.search(s.name ?? "");
                }}
              >
                Find
              </Button>
            </span>
          </div>
        ))}
      </div>
      {merge && <MergeDialog from={merge.from} into={merge.into} onClose={() => setMerge(null)} />}
    </div>
  );
}
