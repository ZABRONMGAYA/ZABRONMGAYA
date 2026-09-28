// The timeline canvas: tiny clips (thousands of clips, zoomed out) must draw without canvas errors.
import { describe, expect, it } from "vitest";

import { type Palette, drawTimeline } from "../src/features/timeline/draw";
import { clip, group } from "./fixtures";

/** A 2D context that records paths and rejects what a browser rejects (negative sizes and radii). */
function strictContext() {
  const rects: [number, number, number, number][] = [];
  const reject = (name: string, ...sizes: number[]) => {
    if (sizes.some((v) => v < 0 || !Number.isFinite(v))) throw new Error(`${name}: negative or invalid size`);
  };
  const ctx = {
    setTransform() {},
    clearRect() {},
    fillRect() {},
    beginPath() {},
    rect(x: number, y: number, w: number, h: number) {
      reject("rect", w, h);
      rects.push([x, y, w, h]);
    },
    roundRect(_x: number, _y: number, w: number, h: number, r: number) {
      reject("roundRect", w, h, r);
    },
    fill() {},
    stroke() {},
    clip() {},
    save() {},
    restore() {},
    fillText() {},
    measureText: (text: string) => ({ width: text.length * 6 }),
    set font(_: string) {},
    set textBaseline(_: string) {},
    set fillStyle(_: string) {},
    set strokeStyle(_: string) {},
    set lineWidth(_: number) {},
  };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, rects };
}

const palette = new Proxy({}, { get: () => "#000" }) as Palette;

describe("drawTimeline", () => {
  it("draws a selected, reviewed clip narrower than its outline", () => {
    // 0.2 s at 2 px/s is 0.4 px wide: its 2 px outline would have a negative width.
    const tiny = clip(1, 10, 0.2, { status: "needs_review", flags: ["manual"] });
    const long = clip(2, 0, 600, { method: "reference" });
    const { ctx, rects } = strictContext();
    const stats = drawTimeline({
      ctx,
      dpr: 1,
      width: 1000,
      height: 200,
      view: { pxPerSec: 2, startS: 0 },
      group: group([tiny, long]),
      palette,
      selected: 1,
      anchorId: 2,
      drag: null,
      cursorS: null,
      peaksFor: () => null,
    });
    expect(stats.clips).toBe(2);
    expect(rects.every(([, , w, h]) => w >= 0 && h >= 0)).toBe(true);
  });
});
