// Clips the editor should check, most severe first (the engine decides the order). Only the rows on screen are
// drawn, so the list stays quick with thousands of entries.
import type { ReviewItem, Timeline } from "../../api/contract";
import { REASON_LABELS } from "../../lib/labels";
import { useViewport, visibleRange } from "../../lib/virtual";
import { findClip, usePick } from "../../state/store";

const ROW_H = 44;

export function ReviewQueue() {
  const timeline = usePick("timeline").timeline;
  // Before the first synchronisation every clip would be listed; the timeline explains the next step instead.
  if (!timeline || timeline.groups.length === 0) return null;
  const items = timeline.review;

  return (
    <section className="review" data-testid="review-queue">
      <h2>
        To review <span className="count">{items.length.toLocaleString()}</span>
      </h2>
      {items.length === 0 ? (
        <p className="muted">Every clip is placed confidently.</p>
      ) : (
        <ReviewList timeline={timeline} items={items} />
      )}
    </section>
  );
}

function ReviewList({ timeline, items }: { timeline: Timeline; items: ReviewItem[] }) {
  const { selected, select, reveal } = usePick("selected", "select", "reveal");
  const viewport = useViewport();
  const { first, last, total } = visibleRange(items.length, ROW_H, viewport.top, viewport.height);
  return (
    <div className="review-list" ref={viewport.ref} onScroll={viewport.onScroll}>
      <ul style={{ height: total }}>
        {items.slice(first, last).map((item, k) => {
          const clip = findClip(timeline, item.clip_id);
          const name = clip?.name ?? `Clip ${item.clip_id}`;
          return (
            <li
              key={item.clip_id}
              className={`review-item ${item.reason} ${selected === item.clip_id ? "selected" : ""}`}
              style={{ top: (first + k) * ROW_H, height: ROW_H - 4 }}
              onClick={() => {
                select(item.clip_id);
                reveal(item.clip_id);
              }}
              title={clip ? `${clip.device_name} · ${clip.path}` : undefined}
              data-testid={`review-${name}`}
            >
              <span className="clip-name">
                {name}
                {clip && <span className="muted"> · {clip.device_name}</span>}
              </span>
              <span className="reason">{REASON_LABELS[item.reason]}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
