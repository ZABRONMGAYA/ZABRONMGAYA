// The Sync workspace (S06): the multicamera viewer and the Sync inspector above the timeline, all driven by one
// master clock. Space plays, ←/→ step a frame, , and . nudge the selected clip (⇧ ten frames, ⌥ a millisecond),
// S sets a sync point, ⌘L locks the clip where it is, ⌘⌫ resets it, 1–9 pick a camera, Enter shows it large,
// M mutes the monitor, F goes full screen.
import { useEffect } from "react";

import { parseRate } from "../../lib/format";
import { usePlayback } from "../../state/playback";
import { displayedGroup, findClip, useApp, usePick } from "../../state/store";
import { ReviewBar } from "./ReviewBar";
import { TimelinePanel } from "../timeline/TimelinePanel";
import { anglesOf, visibleAngles } from "./angles";
import { clock } from "./clock";
import "./probe";
import { MulticamViewer, audioMonitor, useGroupClock } from "./MulticamViewer";
import { SyncInspector, addSyncPoint } from "./SyncInspector";

function isTyping(): boolean {
  const el = document.activeElement;
  return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}

/** Frame rate the playhead steps by: the selected clip's, else the reference's, else 25. */
function stepRate(): number {
  const { timeline, selected } = useApp.getState();
  const clip = findClip(timeline, selected);
  const g = displayedGroup(useApp.getState());
  const ref = g?.clips.find((c) => c.clip_id === timeline?.reference_clip_id) ?? g?.clips.find((c) => c.has_video);
  return parseRate(clip?.frame_rate ?? ref?.frame_rate ?? null) ?? 25;
}

function useWorkspaceKeys(): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping() || !useApp.getState().project) return;
      const app = useApp.getState();
      const pb = usePlayback.getState();
      const mod = e.metaKey || e.ctrlKey;
      const clip = findClip(app.timeline, app.selected);
      const frame = 1 / (parseRate(clip?.frame_rate ?? null) ?? 25);
      const key = e.key;
      // Enter and Space activate a focused button (the timeline's clip list, the inspector's actions) as usual.
      const focused = document.activeElement;
      if (
        (key === "Enter" || key === " ") &&
        (focused instanceof HTMLButtonElement || focused instanceof HTMLAnchorElement)
      )
        return;
      if (key === " " && !mod) {
        e.preventDefault();
        audioMonitor(); // created from a user gesture, so its AudioContext may start
        clock.toggle();
      } else if ((key === "ArrowLeft" || key === "ArrowRight") && !mod && !e.altKey) {
        e.preventDefault();
        clock.step((key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 10 : 1), stepRate());
      } else if ((key === "," || key === "." || key === "<" || key === ">") && !mod && clip?.start_s !== null && clip) {
        e.preventDefault();
        const dir = key === "," || key === "<" ? -1 : 1;
        const step = e.altKey ? 0.001 : e.shiftKey || key === "<" || key === ">" ? 10 * frame : frame;
        app.nudge(clip.clip_id, dir * step);
      } else if (key.toLowerCase() === "s" && !mod && !e.shiftKey && clip) {
        e.preventDefault();
        void addSyncPoint(clip);
      } else if (mod && key.toLowerCase() === "l" && clip) {
        e.preventDefault();
        void app.confirm(clip.clip_id);
      } else if (mod && key === "Backspace" && clip) {
        e.preventDefault();
        void app.correct("clear_offset", clip.clip_id);
      } else if (key === "k" && !mod) {
        clock.pause();
      } else if (key === "l" && !mod) {
        if (!clock.playing) clock.play();
        else clock.setRate(clock.rate >= 2 ? 1 : 2);
      } else if (key === "m" && !mod) {
        pb.setMonitor(pb.monitor === "mute" ? null : "mute");
      } else if (key === "f" && !mod) {
        const el = document.querySelector<HTMLElement>("[data-testid=multicam-viewer]");
        void (document.fullscreenElement ? document.exitFullscreen() : el?.requestFullscreen());
      } else if (key === "Enter" && !mod && pb.active) {
        e.preventDefault();
        pb.setProgram(pb.active);
        pb.setLayout(pb.layout === "program" ? "grid" : "program");
      } else if (/^[1-9]$/.test(key) && !mod) {
        const g = displayedGroup(app);
        if (!g) return;
        const { shown } = visibleAngles(anglesOf(g), {
          hidden: pb.hidden,
          pinned: pb.pinned,
          solo: pb.solo,
          page: pb.page,
        });
        const angle = shown[Number(key) - 1];
        if (!angle) return;
        pb.setActive(angle.key);
        if (pb.layout === "program") pb.setProgram(angle.key);
        const T = clock.now();
        const at =
          angle.clips.find((c) => c.start_s !== null && T >= c.start_s && T < c.start_s + c.duration_s) ??
          angle.clips[0];
        if (at) app.select(at.clip_id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

/** The playhead and the timeline's edit cursor are one: a seek moves the cursor, a click on the timeline seeks. */
function useCursorFollowsClock(): void {
  useEffect(
    () =>
      clock.subscribe((e) => {
        if (e === "pause" || e === "seek") {
          const t = clock.now();
          if (useApp.getState().cursorS !== t) useApp.setState({ cursorS: t });
        }
      }),
    [],
  );
}

export function SyncWorkspace() {
  const { timeline, group } = usePick("timeline", "group");
  const current = displayedGroup({ timeline, group });
  useGroupClock(current);
  useWorkspaceKeys();
  useCursorFollowsClock();
  useEffect(() => {
    window.mcsyncMulticam = {
      clock,
      monitor: () => ({ chunks: audioMonitor().chunks, path: audioMonitor().lastPath }),
      tiles: () => usePlayback.getState().stats,
    };
    return () => {
      clock.pause();
      delete window.mcsyncMulticam;
    };
  }, []);
  const review = usePlayback((s) => s.review);
  const viewer = usePlayback((s) => s.viewer);

  if (!viewer)
    // The timeline alone, the inspector beside it (more room for many tracks).
    return (
      <div className="workspace sy-ws" data-testid="workspace">
        <div className="panes">
          <TimelinePanel />
          <SyncInspector />
        </div>
      </div>
    );

  return (
    <div className="workspace sy-ws" data-testid="workspace">
      <div className="sy-ws__top">
        {current ? (
          <div className="sy-ws__viewer">
            <MulticamViewer group={current} />
            {review && <ReviewBar />}
          </div>
        ) : (
          <div className="sy-ws__viewer sy-ws__viewer--empty" />
        )}
        <SyncInspector />
      </div>
      <div className="panes sy-ws__timeline">
        <TimelinePanel />
      </div>
    </div>
  );
}
