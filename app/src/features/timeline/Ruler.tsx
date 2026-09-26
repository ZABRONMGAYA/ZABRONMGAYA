// Time ruler above the tracks.
import { useEffect, useRef } from "react";

import { formatTime, rulerStep } from "../../lib/format";
import { type View, timeToX, xToTime } from "./geometry";

export function Ruler({ view, width, height }: { view: View; width: number; height: number }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const dpr = window.devicePixelRatio || 1;

  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d");
    if (!el || !ctx) return;
    const w = Math.max(1, Math.round(width * dpr));
    const h = Math.max(1, Math.round(height * dpr));
    if (el.width !== w) el.width = w; // resizing reallocates the canvas
    if (el.height !== h) el.height = h;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const style = getComputedStyle(el);
    ctx.strokeStyle = style.borderBottomColor;
    ctx.fillStyle = style.color;
    ctx.font = `11px ${style.fontFamily}`;
    ctx.textBaseline = "top";

    const step = rulerStep(view.pxPerSec);
    const minor = step / 5;
    const digits = step < 0.1 ? 2 : step < 1 ? 1 : 0;
    const first = Math.floor(xToTime(0, view) / minor) * minor;
    const last = xToTime(width, view);
    ctx.beginPath();
    for (let i = 0; first + i * minor <= last; i++) {
      const t = first + i * minor;
      const x = Math.round(timeToX(t, view)) + 0.5;
      const major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
      ctx.moveTo(x, height);
      ctx.lineTo(x, major ? height - 10 : height - 4);
      if (major) ctx.fillText(formatTime(Math.round(t / step) * step, digits), x + 3, 4);
    }
    ctx.moveTo(0, height - 0.5);
    ctx.lineTo(width, height - 0.5);
    ctx.stroke();
  }, [view, width, height, dpr]);

  return <canvas ref={canvas} className="ruler" style={{ width, height }} />;
}
