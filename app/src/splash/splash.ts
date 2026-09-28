// S00 splash: the signature logo reveal (Motion Specification §1), then startup steps reported by the main process
// (status and percentage are real load steps, never a timer).
import "../design-system/tokens.css";
import "../design-system/fonts.css";
import "./splash.css";

interface Step {
  text: string;
  percent: number;
  failed?: boolean;
}

declare global {
  interface Window {
    /** Called by the main process for each startup step. */
    splashStep(step: Step): void;
  }
}

const $ = (id: string) => document.getElementById(id)!;

window.splashStep = ({ text, percent, failed }) => {
  $("status").textContent = text;
  $("percent").textContent = `${Math.round(percent)}%`;
  $("fill").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  document.querySelector(".splash")!.classList.toggle("splash--failed", Boolean(failed));
  if (percent >= 100) document.querySelector(".splash")!.setAttribute("aria-busy", "false");
};

const version = new URLSearchParams(location.search).get("version");
$("version").textContent = version ? `Version ${version}` : "";

// --- logo reveal: bars arrive from both sides and align on the sync line, then the wordmark wipes in.
const START_OFFSETS = [-30, 22, -44, 14]; // viewBox units
const standard = (x: number) => 1 - Math.pow(1 - x, 3); // ease-out cubic (≈ cubic-bezier(.2,0,0,1))
const bars = [...document.querySelectorAll<SVGRectElement>(".splash__symbol .bar")];
const line = document.querySelector<SVGRectElement>(".splash__symbol .line")!;
const wordmark = document.querySelector<HTMLElement>(".splash__wordmark")!;

function frame(t: number): void {
  const p = Math.max(0, (t - 0.3) / 1.2);
  bars.forEach((bar, i) => {
    const q = standard(Math.min(1, Math.max(0, (p - i * 0.08) / 0.7)));
    bar.setAttribute("transform", `translate(${START_OFFSETS[i]! * (1 - q)} 0)`);
    bar.style.opacity = String(0.35 + 0.65 * q);
  });
  line.style.opacity = t >= 1.5 ? "1" : "0"; // hard cut
  const r = standard(Math.min(1, Math.max(0, (t - 1.8) / 0.6)));
  wordmark.style.opacity = String(r);
  wordmark.style.transform = `translateX(${-16 * (1 - r)}px)`;
  wordmark.style.clipPath = `inset(0 ${100 * (1 - r)}% 0 0)`;
}

if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
  frame(10);
} else {
  const start = performance.now();
  const tick = (now: number) => {
    const t = (now - start) / 1000;
    frame(t);
    if (t < 2.4) requestAnimationFrame(tick);
  };
  frame(0);
  requestAnimationFrame(tick);
}
