"""Benchmark the synchronisation engine on synthetic recordings.

Usage::

    python scripts/benchmark_sync.py                   # 10 min, 1 h and 3 h references
    python scripts/benchmark_sync.py --hours 0.5 2     # custom reference lengths
    python scripts/benchmark_sync.py --multicam 24     # full engine run over 24 clips

Scene synthesis is excluded from the timings; only engine work is measured
(signal preparation from 8 kHz PCM, envelope extraction, matching, solving).
"""

from __future__ import annotations

import argparse
import resource
import sys
import time

import numpy as np

from mcsync.sync import ClipInput, MatchStatus, SyncEngine, SyncOptions, estimate_offset, prepare_signal
from mcsync.testing.synthetic import make_scene, record


def peak_rss_mb() -> float:
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return kb / 1024 if sys.platform != "darwin" else kb / 1024 / 1024


def bench_pairwise(hours: float, clip_minutes: float) -> None:
    duration = hours * 3600.0
    scene = make_scene(duration + 120.0, kind="mixed", rate=8000, seed=7)
    ref_pcm = record(scene, start_s=60.0, duration_s=duration, rate=8000, seed=1)
    clip_pcm = record(
        scene,
        start_s=60.0 + duration * 0.6,
        duration_s=clip_minutes * 60.0,
        rate=8000,
        clock_ppm=20.0,
        snr_db=5,
        seed=2,
    )

    t0 = time.perf_counter()
    ref = prepare_signal(ref_pcm, 8000)
    clip = prepare_signal(clip_pcm, 8000)
    t1 = time.perf_counter()
    ref.envelope()
    clip.envelope()
    t2 = time.perf_counter()
    est = estimate_offset(ref, clip)
    t3 = time.perf_counter()
    err_ms = (
        (est.offset_s - (duration * 0.6 - 20e-6 * est.offset_time_s)) * 1e3
        if est.offset_s is not None
        else float("nan")
    )
    print(
        f"ref {hours:5.2f} h vs clip {clip_minutes:4.0f} min | prepare {t1 - t0:6.2f} s | envelopes {t2 - t1:5.2f} s | "
        f"match {t3 - t2:5.2f} s | {est.status.value:9s} err {err_ms:+.3f} ms drift {est.drift_ppm:5.1f} ppm | "
        f"peak RSS {peak_rss_mb():6.0f} MB"
    )


def bench_multicam(n_clips: int) -> None:
    rng = np.random.default_rng(0)
    scene = make_scene(3700.0, kind="mixed", rate=8000, seed=11)
    clips = [ClipInput("recorder", audio=prepare_signal(record(scene, start_s=50.0, duration_s=3600.0, seed=1), 8000))]
    for k in range(n_clips - 1):
        start = float(rng.uniform(0.0, 3500.0))
        dur = float(rng.uniform(30.0, 600.0))
        pcm = record(
            scene, start_s=start, duration_s=min(dur, 3690.0 - start), snr_db=float(rng.uniform(0, 20)), seed=k
        )
        clips.append(ClipInput(f"cam{k % 4}_{k:03d}", audio=prepare_signal(pcm, 8000), device_id=f"cam{k % 4}"))
    engine = SyncEngine(SyncOptions(reference_clip_id="recorder"))
    t0 = time.perf_counter()
    matches = engine.analyze(clips)
    t1 = time.perf_counter()
    result = engine.solve(clips, matches)
    t2 = time.perf_counter()
    confident = sum(m.status == MatchStatus.CONFIDENT for m in matches)
    synced = sum(p.status.value == "synced" for p in result.placements.values())
    print(
        f"{n_clips} clips, {len(matches)} pairs ({confident} confident) | analyze {t1 - t0:6.2f} s "
        f"({(t1 - t0) / max(len(matches), 1) * 1e3:5.1f} ms/pair) | solve {(t2 - t1) * 1e3:6.1f} ms | "
        f"{synced}/{n_clips} synced | peak RSS {peak_rss_mb():6.0f} MB"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, nargs="*", default=[1 / 6, 1.0, 3.0])
    parser.add_argument("--clip-minutes", type=float, default=20.0)
    parser.add_argument("--multicam", type=int, default=0, help="also benchmark a full run over N clips")
    args = parser.parse_args()
    for hours in args.hours:
        bench_pairwise(hours, min(args.clip_minutes, hours * 60 * 0.3))
    if args.multicam:
        bench_multicam(args.multicam)


if __name__ == "__main__":
    main()
