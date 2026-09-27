"""Scalability test: a whole production through the engine service, measured.

    python -m mcsync.testing.stress PRODUCTION_DIR WORK_DIR [--out report.json]

``PRODUCTION_DIR`` is a production written by :func:`mcsync.testing.production.generate_production`. The test drives
the same service the desktop app uses, the way an editor would: import everything, pause and resume the analysis,
quit in the middle and reopen (resume), synchronise, look at results (index, filters, timeline, summary), save and
reopen, and synchronise again. It records wall time per stage, memory and processor use of the engine and its
worker processes, database and cache sizes, and scores every placement against the production's truth.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from mcsync import resources
from mcsync.service.app import EngineService

from .production import Production, evaluate


class Sampler(threading.Thread):
    """Memory of this process and its children, and machine-wide processor use, twice a second (Linux /proc;
    elsewhere only this process's peak memory)."""

    def __init__(self, interval: float = 0.5) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.stop_event = threading.Event()
        self.peak_rss = 0
        self.samples: list[tuple[float, int, float]] = []  # (time, tree rss, cpu busy fraction)
        self._page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

    def _tree_rss(self) -> int:
        me = os.getpid()
        if not os.path.isdir("/proc"):
            return resources.current_rss_bytes() or 0
        children: dict[int, list[int]] = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as f:
                    ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                children.setdefault(ppid, []).append(int(entry))
            except (OSError, ValueError, IndexError):
                continue
        total, stack = 0, [me]
        while stack:
            pid = stack.pop()
            try:
                with open(f"/proc/{pid}/statm") as f:
                    total += int(f.read().split()[1]) * self._page
            except (OSError, ValueError):
                pass
            stack += children.get(pid, [])
        return total

    @staticmethod
    def _cpu() -> tuple[int, int] | None:
        try:
            with open("/proc/stat") as f:
                values = [int(v) for v in f.readline().split()[1:]]
            idle = values[3] + values[4]
            return sum(values) - idle, sum(values)
        except (OSError, ValueError):
            return None

    def run(self) -> None:
        last = self._cpu()
        t0 = time.monotonic()
        while not self.stop_event.wait(self.interval):
            rss = self._tree_rss()
            self.peak_rss = max(self.peak_rss, rss)
            now = self._cpu()
            busy = 0.0
            if now and last and now[1] > last[1]:
                busy = (now[0] - last[0]) / (now[1] - last[1])
            last = now
            self.samples.append((time.monotonic() - t0, rss, busy))

    def summary(self, since: float = 0.0, until: float = float("inf")) -> dict:
        window = [s for s in self.samples if since <= s[0] <= until]
        if not window:
            return {}
        return {
            "peak_rss_mb": round(max(s[1] for s in window) / 2**20, 1),
            "mean_rss_mb": round(sum(s[1] for s in window) / len(window) / 2**20, 1),
            "mean_cpu_percent": round(100 * sum(s[2] for s in window) / len(window), 1),
        }


def du(path: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def run(production_dir: Path, work: Path) -> dict:
    prod = Production.load(production_dir)
    work.mkdir(parents=True, exist_ok=True)
    cache = work / "cache"
    project_path = work / "Festival.syncora"
    for p in (project_path, Path(str(project_path) + "-wal"), Path(str(project_path) + "-shm")):
        p.unlink(missing_ok=True)
    shutil.rmtree(cache, ignore_errors=True)

    sampler = Sampler()
    sampler.start()
    t_start = time.monotonic()
    marks: dict[str, float] = {}
    events: list[dict] = []

    def mark(name: str) -> None:
        marks[name] = round(time.monotonic() - t_start, 2)
        print(f"[{marks[name]:8.1f}s] {name}", file=sys.stderr, flush=True)

    def notify(method: str, params) -> None:  # noqa: ANN001
        if method == "pipeline.sync_finished":
            events.append({"t": time.monotonic() - t_start, **{k: v for k, v in params.items() if k != "timeline"}})

    svc = EngineService(notify=notify, cache_dir=str(cache))
    report: dict = {
        "machine": resources.detect().to_dict(),
        "workers": svc.plan.to_dict(),
        "files": {
            "video": sum(f.kind == "video" for f in prod.files.values()),
            "audio": sum(f.kind == "audio" for f in prod.files.values()),
            "other": sum(f.kind == "other" for f in prod.files.values()),
            "media_bytes": du(prod.root),
        },
        "plan": prod.plan.__dict__,
    }

    def wait(predicate, timeout: float, what: str) -> None:  # noqa: ANN001
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise TimeoutError(what)
            time.sleep(0.2)

    def busy(*kinds: str) -> int:
        return svc.project.pending_count(list(kinds)) + sum(
            1 for r in svc.pipeline.snapshot()["running"] if r["kind"] in kinds
        )

    def analysed() -> int:
        return svc.project.task_counts().get("analyze", {}).get("done", 0)

    # --- import --------------------------------------------------------------
    svc.project_create(str(project_path), "Festival")
    mark("import started")
    svc.media_add([str(prod.root)])
    wait(lambda: not svc.pipeline.import_request(1).walking, 3600, "discovery")
    mark("discovery done")
    report["discovered"] = svc.project.discovery_counts()

    # Pause partway through the analysis, then resume.
    n_audio_clips = report["files"]["video"] + report["files"]["audio"]
    wait(lambda: analysed() >= 0.3 * n_audio_clips or not busy("probe", "analyze"), 7200, "30% analysed")
    mark("pause requested")
    svc.pipeline_pause()
    t_pause = time.monotonic()
    wait(lambda: not svc.pipeline.snapshot()["running"], 600, "pause")
    report["pause_latency_s"] = round(time.monotonic() - t_pause, 2)
    before = analysed()
    time.sleep(3)
    report["work_done_while_paused"] = analysed() - before
    svc.pipeline_resume()
    mark("resumed")

    # Quit in the middle of the analysis and reopen: the queue resumes where it stopped.
    wait(lambda: analysed() >= 0.6 * n_audio_clips or not busy("probe", "analyze"), 7200, "60% analysed")
    done_before_quit = analysed()
    t0 = time.monotonic()
    svc.project_close()
    report["close_during_analysis_s"] = round(time.monotonic() - t0, 2)
    mark("closed during analysis")
    t0 = time.monotonic()
    info = svc.project_open(str(project_path))
    report["reopen_during_analysis_s"] = round(time.monotonic() - t0, 2)
    report["resume_offer"] = info["resume"]
    report["analysed_kept_after_reopen"] = analysed() >= done_before_quit
    svc.pipeline_resume()
    mark("resumed after reopen")
    wait(lambda: not busy("probe", "analyze"), 7200, "analysis")
    mark("analysis done")
    report["tasks_after_analysis"] = svc.project.task_counts()

    # --- sync ----------------------------------------------------------------
    run_id = svc.sync_start()["run_id"]
    mark("sync started")
    phases: dict[str, float] = {}

    def phase_done() -> bool:
        state = svc.pipeline.sync_state or {}
        phase = state.get("phase")
        if phase and phase not in phases:
            phases[phase] = round(time.monotonic() - t_start, 2)
            mark(f"phase {phase}")
        return phase == "done" or any(e.get("run_id") == run_id and e["status"] != "completed" for e in events)

    wait(phase_done, 4 * 3600, "sync")
    mark("sync done")
    state = svc.pipeline.sync_state or {}
    report["sync"] = {
        "run_id": run_id,
        "strategy": state.get("strategy"),
        "pairs_planned": state.get("planned"),
        "pairs_reused": state.get("reused"),
        "unmatched_after_candidates": state.get("unmatched"),
        "extended_searches": state.get("extended"),
        "phases_at_s": phases,
        "tasks": svc.project.task_counts(run_id=run_id),
    }

    # --- results, like the interface uses them -------------------------------------
    timings: dict[str, float] = {}

    def timed(name: str, fn):  # noqa: ANN001, ANN202
        t0 = time.perf_counter()
        value = fn()
        timings[name] = round(time.perf_counter() - t0, 3)
        return value

    index = timed("media.index (every clip, with status)", svc.media_index)
    timings["media.index payload MB"] = round(len(json.dumps(index)) / 2**20, 2)
    cols = {c: k for k, c in enumerate(index["columns"])}
    rows = index["rows"]
    timed("search: name contains 'CAMA'", lambda: [r for r in rows if "CAMA" in (r[cols["path"]] or "")])
    timed("filter: recorded 10:30-12:00", lambda: [r for r in rows if r[cols["creation_time"]] and
                                                 "T10:30" <= r[cols["creation_time"]][10:16] <= "T12:00"])  # fmt: skip
    timed("filter: unsynchronized", lambda: [r for r in rows if r[cols["category"]] in ("manual", "pending")])
    timed("filter: confidence < 80%", lambda: [r for r in rows if (r[cols["confidence"]] or 0) < 0.8])
    timed("sort: by recording time", lambda: sorted(rows, key=lambda r: r[cols["creation_time"]] or ""))
    timeline = timed("timeline.get", svc.timeline_get)
    timings["timeline payload MB"] = round(len(json.dumps(timeline)) / 2**20, 2)
    summary = timed("sync.summary", svc.sync_summary)
    report["summary"] = {k: summary[k] for k in ("clips", "counts", "unreadable_files")}
    report["summary"]["sessions"] = len(summary["sessions"])
    report["summary"]["sources"] = len(summary["sources"])
    report["accuracy"] = evaluate(prod, index)

    # --- save and reopen ------------------------------------------------------------
    timed("project.close (save)", svc.project_close)
    reopened = timed("project.open (reopen, checks every file)", lambda: svc.project_open(str(project_path)))
    timed("clip list after reopen", svc.project.clips)
    report["reopened_clips"] = reopened["clips"]

    # --- synchronise again: nothing changed, everything is reused ---------------------
    t0 = time.monotonic()
    again = svc.sync_start()["run_id"]
    wait(lambda: (svc.pipeline.sync_state or {}).get("phase") == "done" and svc.pipeline.sync_state["run_id"] == again,
         3600, "second sync")  # fmt: skip
    counts = svc.project.task_counts(run_id=again)
    report["resync"] = {
        "seconds": round(time.monotonic() - t0, 2),
        "verified_again": sum(counts.get(k, {}).get("done", 0) for k in ("match", "extend")),
        "reused": (svc.pipeline.sync_state or {}).get("reused"),
    }
    report["ui_timings_s"] = timings
    mark("finished")

    stats = svc.project.database_stats()
    report["database"] = {"bytes": stats["bytes"], "wal_bytes": stats["wal_bytes"], "rows": stats["rows"]}
    report["cache_bytes"] = du(cache)
    report["cache_index_bytes"] = du(cache / "index")
    svc.close()
    sampler.stop_event.set()
    sampler.join()
    report["marks_s"] = marks
    report["resources"] = {
        "whole_run": sampler.summary(),
        "analysis": sampler.summary(marks.get("import started", 0), marks.get("analysis done", 1e9)),
        "sync": sampler.summary(marks.get("sync started", 0), marks.get("sync done", 1e9)),
        "gpu": "not used (analysis runs on the processor)",
    }
    report["total_s"] = marks["finished"]
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("production", type=Path)
    ap.add_argument("work", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    report = run(args.production, args.work)
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
