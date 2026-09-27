// Windowed rendering for lists and grids of thousands of items: only what is on screen (plus a margin) is in the
// DOM, whatever the project size.
import { type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

export interface Viewport {
  ref: RefObject<HTMLDivElement | null>;
  top: number;
  height: number;
  width: number;
  onScroll: () => void;
}

export function useViewport(): Viewport {
  const ref = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState({ top: 0, height: 600, width: 1000 });
  const frame = useRef(0);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setBox({ top: el.scrollTop, height: el.clientHeight, width: el.clientWidth });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  const onScroll = useCallback(() => {
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      const el = ref.current;
      if (el) setBox((b) => (b.top === el.scrollTop ? b : { ...b, top: el.scrollTop }));
    });
  }, []);

  return { ref, ...box, onScroll };
}

/** Rows [first, last) to render for a list of ``count`` rows of ``rowHeight`` px. */
export function visibleRange(count: number, rowHeight: number, top: number, height: number, overscan = 8) {
  const first = Math.max(0, Math.floor(top / rowHeight) - overscan);
  const last = Math.min(count, Math.ceil((top + height) / rowHeight) + overscan);
  return { first, last, total: count * rowHeight };
}

/** Grid layout: how many columns fit, and the height of one row of cards. */
export function gridLayout(width: number, minCard: number, gap: number, padding: number, bodyHeight: number) {
  const inner = Math.max(0, width - 2 * padding);
  const columns = Math.max(1, Math.floor((inner + gap) / (minCard + gap)));
  const cardWidth = (inner - gap * (columns - 1)) / columns;
  const rowHeight = Math.round((cardWidth * 9) / 16 + bodyHeight + gap);
  return { columns, cardWidth, rowHeight };
}

/** Scroll so that row ``index`` is visible. */
export function scrollIntoView(el: HTMLElement | null, index: number, rowHeight: number): void {
  if (!el) return;
  const top = index * rowHeight;
  if (top < el.scrollTop) el.scrollTop = top;
  else if (top + rowHeight > el.scrollTop + el.clientHeight) el.scrollTop = top + rowHeight - el.clientHeight;
}
