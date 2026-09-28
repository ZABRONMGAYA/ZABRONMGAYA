// S07 AI sync: a clip audio fingerprints could not place is looked for by what was said, the light changes both
// cameras saw, its audio compared again at close range and the camera clocks (Sync UI specification §3).
import { Check, Lock, Play, ScanEye, TriangleAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { AiCandidate, EvidenceLane } from "../../api/contract";
import {
  Button,
  ConfidenceBar,
  EmptyState,
  Eyebrow,
  SyncBadge,
  shortcut,
  statusFor,
} from "../../design-system/components";
import { formatTime } from "../../lib/format";
import { useAnalyze } from "../../state/analyze";
import { useProd } from "../../state/production";
import { useApp } from "../../state/store";
import { PreviewDialog } from "./PreviewDialog";

const LANES: { lane: EvidenceLane; label: string; desc: string }[] = [
  { lane: "audio", label: "Audio similarity", desc: "the sound compared again within ±2 s" },
  { lane: "speech", label: "Speech similarity", desc: "the same sentences heard in both" },
  { lane: "visual", label: "Visual similarity", desc: "the same flashes and light changes" },
  { lane: "metadata", label: "Metadata", desc: "the camera clocks" },
];

const METHOD_WORDS: Record<EvidenceLane, string> = {
  speech: "Speech",
  visual: "Visual event",
  audio: "Audio",
  metadata: "Clock",
};

/** "+01:12.800" (hours prefixed from an hour on). */
export function signedTime(seconds: number): string {
  return `${seconds < 0 ? "−" : "+"}${formatTime(Math.abs(seconds))}`;
}

export function AiSync() {
  const clipId = useAnalyze((s) => s.aiClip);
  const result = useAnalyze((s) => s.aiResult);
  const jobId = useAnalyze((s) => s.aiJob);
  const k = useAnalyze((s) => s.aiCandidate);
  const job = useApp((s) => (jobId ? s.jobs[jobId] : undefined));
  const rows = useProd((s) => s.rows);
  const threshold = useApp((s) => s.project?.settings.review_threshold ?? 0.85);
  const [preview, setPreview] = useState(false);
  const byId = useMemo(() => new Map(rows.map((r) => [r.clip_id, r])), [rows]);
  const clip = clipId !== null ? byId.get(clipId) : undefined;
  const candidates = result?.candidates ?? [];
  const best: AiCandidate | undefined = candidates[Math.min(k, candidates.length - 1)];
  const anchor = best ? byId.get(best.anchor_clip_id) : undefined;
  const searching = jobId !== null;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (preview) return;
      const a = useAnalyze.getState();
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && best) {
        e.preventDefault();
        void a.acceptAi();
      } else if (e.altKey && e.key === "ArrowRight" && candidates.length > 1) {
        e.preventDefault();
        a.setCandidate((a.aiCandidate + 1) % candidates.length);
      } else if (e.key === "Escape") {
        e.preventDefault();
        void a.rejectAi();
      } else if (e.key === " " && best && !(document.activeElement instanceof HTMLButtonElement)) {
        e.preventDefault();
        setPreview(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [best, candidates.length, preview]);

  if (clipId === null) {
    return (
      <div className="sy-aisync sy-aisync--empty">
        <EmptyState
          icon={ScanEye}
          title="Choose a clip"
          body="Open AI sync from a clip that needs review or could not be placed: select it on the timeline and press Find with AI (⇧A)."
        />
      </div>
    );
  }

  const name = clip?.name ?? `Clip ${clipId}`;
  const synced =
    clip?.category === "synchronized" || clip?.category === "high_confidence" || clip?.category === "confirmed";
  const duration = result?.duration_s ?? clip?.duration_s ?? 0;
  const status = best ? statusFor(best.confidence, threshold) : null;
  const lanes = best ? LANES.filter((l) => best.lanes[l.lane] || best.evidence.some((e) => e.lane === l.lane)) : LANES;
  const span = best ? candidateSpan(best) : null;

  return (
    <div className="sy-aisync" data-testid="ai-sync">
      <main className="sy-aisync__main">
        <div className="sy-banner sy-aisync__banner" role="note">
          {synced ? (
            <Check size={16} className="sy-ok" aria-hidden />
          ) : (
            <TriangleAlert size={16} className="sy-banner__icon" aria-hidden />
          )}
          <div>
            <div className="sy-banner__title">
              {synced ? "Already synchronized" : "Traditional synchronization unavailable"}
            </div>
            <div className="sy-banner__body">
              {result?.reason ?? `${name} could not be placed from its audio alone.`}
            </div>
          </div>
        </div>

        {searching ? (
          <Eyebrow className="sy-aisync__eyebrow">
            <ScanEye size={16} aria-hidden /> AI is searching for matching moments…
            <span className="sy-muted">{job?.message}</span>
          </Eyebrow>
        ) : result ? (
          <Eyebrow className="sy-aisync__eyebrow">
            <ScanEye size={16} aria-hidden /> AI searched {formatTime(result.searched.speech_s, 0)} of speech in {name}{" "}
            against {result.searched.against} placed recording{result.searched.against === 1 ? "" : "s"}
          </Eyebrow>
        ) : null}

        <h1 className="sy-aisync__title">Evidence across {lanes.length} signals</h1>

        <div className={`sy-lanes ${searching ? "sy-lanes--scanning" : ""}`} data-testid="evidence-lanes">
          <div className="sy-lanes__axis tnum">
            <span />
            <span className="sy-lanes__ticks">
              <span>{signedTime(0).slice(0, -4)}</span>
              <span>{signedTime(duration / 2).slice(0, -4)}</span>
              <span>{signedTime(duration).slice(0, -4)}</span>
            </span>
            <span />
          </div>
          {lanes.map((l, i) => {
            const cells = best?.lanes[l.lane] ?? [];
            const ev = best?.evidence.find((e) => e.lane === l.lane);
            return (
              <div key={l.lane} className="sy-lane" data-testid={`lane-${l.lane}`}>
                <div>
                  <div className="sy-lane__label">{l.label}</div>
                  <div className="sy-lane__desc">{l.desc}</div>
                </div>
                <div className="sy-lane__track">
                  <div className="sy-lane__cells" aria-label={`${l.label}: ${ev?.note ?? "no evidence"}`}>
                    {Array.from({ length: 48 }, (_, c) => (
                      <span key={c} className={`sy-cell sy-cell--${cells[c] ?? 0}`} />
                    ))}
                  </div>
                  {span && best && (
                    <span
                      className={`sy-lane__span ${i === 0 ? "sy-lane__span--first" : ""} ${i === lanes.length - 1 ? "sy-lane__span--last" : ""}`}
                      style={{ left: `${(span[0] / 48) * 100}%`, width: `${((span[1] - span[0]) / 48) * 100}%` }}
                    >
                      {i === lanes.length - 1 && (
                        <span className="sy-lanes__candidate-label">
                          Candidate {k + 1} · {signedTime(best.offset_s)}
                        </span>
                      )}
                    </span>
                  )}
                </div>
                <div className="sy-lane__note">
                  <span>{ev?.note ?? (searching ? "…" : "no evidence")}</span>
                  {ev && (
                    <>
                      <span className="sy-lane__pct tnum">{Math.round(ev.score * 100)}%</span>
                      <span className="sy-lane__bar">
                        <span style={{ width: `${Math.round(ev.score * 100)}%` }} />
                      </span>
                    </>
                  )}
                </div>
              </div>
            );
          })}
          {searching && <div className="sy-lanes__scan" aria-hidden />}
        </div>

        <div className="sy-lanes__legend">
          <span>
            <i className="sy-cell sy-cell--3" /> Strong
          </span>
          <span>
            <i className="sy-cell sy-cell--2" /> Partial
          </span>
          <span>
            <i className="sy-cell sy-cell--1" /> Weak
          </span>
          <span className="sy-spacer" />
          {candidates.length > 0 && (
            <span>
              {candidates.length} candidate{candidates.length === 1 ? "" : "s"} found · showing{" "}
              {k === 0 ? "best" : `candidate ${k + 1}`}
            </span>
          )}
        </div>
      </main>

      <aside className="sy-aisync__aside" data-testid="ai-result">
        {searching ? (
          <div className="sy-aisync__block">
            <SyncBadge status="running" confidence={job?.progress ?? 0} label="Analyzing" />
            <div className="sy-aisync__source">{name.toUpperCase()}</div>
            <div className="sy-aisync__offset tnum">—</div>
            <ConfidenceBar value={job?.progress ?? 0} tone="running" />
            <p className="sy-muted">{job?.message ?? "Starting…"}</p>
            <Button variant="ghost" size="36" icon={X} onClick={() => void useAnalyze.getState().rejectAi()}>
              Cancel
            </Button>
          </div>
        ) : !result ? (
          <div className="sy-aisync__block">
            <p className="sy-dim">AI sync has not looked for this clip yet.</p>
            <Button variant="primary" size="36" icon={ScanEye} onClick={() => void useAnalyze.getState().startAiSync()}>
              Try AI sync
            </Button>
          </div>
        ) : !best ? (
          <EmptyState
            icon={ScanEye}
            title="No AI results"
            body={`AI found no reliable match for ${name}. Nudge it manually from a clap, flash or cut.`}
            action={
              <div className="sy-aisync__row">
                <Button variant="secondary" size="36" onClick={() => void useAnalyze.getState().rejectAi()}>
                  Manual sync
                </Button>
                <Button variant="ghost" size="36" onClick={() => void useAnalyze.getState().startAiSync()}>
                  Search again
                </Button>
              </div>
            }
          />
        ) : (
          <>
            <div className="sy-aisync__block">
              <span className="sy-aisync__found">
                <Check size={12} aria-hidden /> Match found
              </span>
              <div className="sy-aisync__source">{name.toUpperCase()}</div>
              <div className="sy-aisync__offset tnum" data-testid="ai-offset">
                {signedTime(best.offset_s)}
              </div>
              <div className="sy-muted">relative to {anchor?.name ?? `clip ${best.anchor_clip_id}`}</div>
            </div>
            <dl className="sy-aisync__facts">
              <dt>Confidence</dt>
              <dd>
                <span className="sy-aisync__pct tnum">{Math.round(best.confidence * 100)}%</span>
                <ConfidenceBar value={best.confidence} tone={status ?? "review"} />
              </dd>
              <dt>Method</dt>
              <dd className="sy-aisync__method">
                {best.evidence
                  .filter((e) => e.score > 0)
                  .map((e) => METHOD_WORDS[e.lane])
                  .join(" + ") || "—"}
              </dd>
              <dt>Evidence</dt>
              <dd>
                {best.evidence
                  .filter((e) => e.score > 0)
                  .map((e) => e.note)
                  .join(" · ")}
              </dd>
              {result.agreement !== null && k === 0 && (
                <>
                  <dt>Agreement</dt>
                  <dd>Candidate 1 is {result.agreement.toFixed(1)}× stronger than candidate 2</dd>
                </>
              )}
            </dl>
            {status === "review" && (
              <div className="sy-callout sy-aisync__callout" role="note">
                <TriangleAlert size={20} className="sy-banner__icon" aria-hidden />
                <div>
                  <div className="sy-banner__title">Review recommended · {Math.round(best.confidence * 100)}%</div>
                  <div className="sy-banner__body">
                    Below the review threshold of {Math.round(threshold * 100)}%. Preview the moment before you accept.
                  </div>
                </div>
              </div>
            )}
            <p className="sy-aisync__note">AI results are never locked automatically. Check one moment, then accept.</p>
            <div className="sy-aisync__actions">
              <Button
                variant="primary"
                size="dialog"
                icon={Lock}
                shortcut={shortcut("⌘↵")}
                onClick={() => void useAnalyze.getState().acceptAi()}
                data-testid="ai-accept"
              >
                Accept and lock
              </Button>
              <Button variant="secondary" size="36" icon={Play} shortcut="Space" onClick={() => setPreview(true)}>
                Preview side by side
              </Button>
              <Button
                variant="secondary"
                size="36"
                shortcut={shortcut("⌥→")}
                disabled={candidates.length < 2}
                onClick={() => useAnalyze.getState().setCandidate((k + 1) % candidates.length)}
              >
                Next candidate
              </Button>
              <Button
                variant="ghost"
                size="compact"
                onClick={() => void useAnalyze.getState().rejectAi()}
                data-testid="ai-reject"
              >
                Sync manually instead
              </Button>
            </div>
          </>
        )}
      </aside>
      {preview && best && clip && anchor && (
        <PreviewDialog clip={clip} anchor={anchor} candidate={best} onClose={() => setPreview(false)} />
      )}
    </div>
  );
}

/** The heat cells (0–47) the candidate's evidence spans, for its outline. */
function candidateSpan(c: AiCandidate): [number, number] | null {
  let lo = 48;
  let hi = -1;
  for (const cells of Object.values(c.lanes)) {
    cells?.forEach((v, i) => {
      if (v > 0) {
        lo = Math.min(lo, i);
        hi = Math.max(hi, i);
      }
    });
  }
  return hi >= 0 ? [lo, hi + 1] : null;
}
