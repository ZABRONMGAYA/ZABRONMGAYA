// Clips the editor should check, most severe first (the engine decides the order).
import { REASON_LABELS } from "../../lib/labels";
import { findClip, usePick } from "../../state/store";

export function ReviewQueue() {
  const { timeline, selected, select, reveal } = usePick("timeline", "selected", "select", "reveal");
  if (!timeline) return null;
  const items = timeline.review;

  return (
    <section className="review" data-testid="review-queue">
      <h2>
        To review <span className="count">{items.length}</span>
      </h2>
      {items.length === 0 ? (
        <p className="muted">Every clip is placed confidently.</p>
      ) : (
        <ul>
          {items.map((item) => {
            const clip = findClip(timeline, item.clip_id);
            return (
              <li
                key={item.clip_id}
                className={`review-item ${item.reason} ${selected === item.clip_id ? "selected" : ""}`}
                onClick={() => {
                  select(item.clip_id);
                  reveal(item.clip_id);
                }}
                data-testid={`review-${clip?.name ?? item.clip_id}`}
              >
                <span className="clip-name">{clip?.name ?? `Clip ${item.clip_id}`}</span>
                <span className="reason">{REASON_LABELS[item.reason]}</span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
