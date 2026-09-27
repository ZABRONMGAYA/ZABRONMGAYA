"""Project database benchmark at production scale.

Fills a project with synthetic rows (no media files needed) and times what the app does with it: import, list,
filter, queue, store matches and analysis, search transcripts, reopen. ``python -m mcsync.testing.dbbench --media
10000`` prints a JSON report; SCALABILITY_TEST_REPORT.md records the results.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

from mcsync.media.devices import DeviceGuess
from mcsync.media.library import MediaItem
from mcsync.media.probe import AudioStreamInfo, MediaInfo, TimecodeInfo, VideoStreamInfo
from mcsync.project import Project
from mcsync.resources import peak_rss_bytes
from mcsync.sync.types import MatchStatus, OffsetEstimate, PairwiseMatch

WORDS = (
    "welcome everyone ceremony vows ring kiss applause music dance speech thank you family friends bride groom "
    "toast cake first light camera sound check rolling cut take scene interview question answer"
).split()


def synthetic_items(n_video: int, n_audio: int, root: str = "/Volumes/SHOOT", seed: int = 1) -> list[MediaItem]:
    """Media as a large multi-day production would probe: cameras with rolling clip names, recorders with WAVs."""
    rng = random.Random(seed)
    start = datetime(2026, 6, 14, 8, 0, tzinfo=UTC)
    items = []
    cams = [f"CAM{c}" for c in "ABCDEFGHIJKL"]
    for i in range(n_video):
        cam = cams[i % len(cams)]
        created = start + timedelta(days=i // 2000, seconds=(i % 2000) * 40 + rng.random())
        dur = rng.uniform(20, 900)
        video = VideoStreamInfo(0, "h264", 3840, 2160, Fraction(24000, 1001), None, False, 0.0, dur)
        audio = (AudioStreamInfo(1, "aac", 48000, 2, "stereo", 0.0, dur),)
        info = MediaInfo(
            path=f"{root}/{cam}/DCIM/{cam}_{i:05d}.MP4", size_bytes=int(dur * 12_500_000), mtime_ns=i,
            container="mov,mp4", duration_s=dur, video=(video,), audio=audio, creation_time=created,
            timecode=TimecodeInfo("01:00:00:00", Fraction(24000, 1001), False, 86400) if i % 3 == 0 else None,
            make="Sony", model="FX3", raw={"format": {"filename": "x", "tags": {"k": "v" * 200}}},
        )  # fmt: skip
        items.append(MediaItem(info, f"fp-v{i}", DeviceGuess(cam, cam, "camera"), audio[0]))
    for i in range(n_audio):
        rec = f"REC{i % 8}"
        created = start + timedelta(days=i // 100, seconds=(i % 100) * 800)
        dur = rng.uniform(600, 3600)
        audio = (AudioStreamInfo(0, "pcm_s24le", 48000, 2, "stereo", 0.0, dur),)
        info = MediaInfo(
            path=f"{root}/{rec}/{i:04d}.WAV", size_bytes=int(dur * 288_000), mtime_ns=i, container="wav",
            duration_s=dur, video=(), audio=audio, creation_time=created, make="Zoom", model="F6",
        )  # fmt: skip
        items.append(MediaItem(info, f"fp-a{i}", DeviceGuess(rec, rec, "recorder"), audio[0]))
    return items


def _match(ref: int, tgt: int, rng: random.Random) -> PairwiseMatch:
    conf = rng.random()
    status = MatchStatus.CONFIDENT if conf > 0.85 else MatchStatus.UNCERTAIN if conf > 0.4 else MatchStatus.NO_MATCH
    est = OffsetEstimate(
        offset_s=rng.uniform(-3600, 3600), confidence=conf, status=status, offset_time_s=10.0, drift_ppm=0.0,
        drift_std_ppm=0.0, std_error_s=1e-5, overlap_s=60.0, coarse_psr=10.0, uniqueness=3.0, n_windows=4,
        inlier_fraction=1.0, correlation=0.4,
    )  # fmt: skip
    return PairwiseMatch(str(ref), str(tgt), est.offset_s, est, offset_time_s=10.0)


class Timer:
    def __init__(self) -> None:
        self.results: dict[str, float] = {}

    @contextmanager
    def __call__(self, name: str):
        t0 = time.perf_counter()
        yield
        self.results[name] = round(time.perf_counter() - t0, 4)


def run(n_video: int, n_audio: int, *, n_matches: int, n_transcript: int, n_markers: int, directory: Path) -> dict:
    rng = random.Random(7)
    t = Timer()
    path = directory / f"bench-{n_video + n_audio}.syncora"
    items = synthetic_items(n_video, n_audio)
    n_media = len(items)

    with t("create"):
        project = Project.create(path, "benchmark")
    with t("discover (insert discovered rows)"):
        root = project.add_import_root("/Volumes/SHOOT")
        project.add_discovered([(it.path, it.info.size_bytes, it.info.mtime_ns, "video", root) for it in items])
    with t("insert media (batch of 500)"):
        clip_ids: list[int] = []
        for start in range(0, n_media, 500):
            clip_ids += project.add_media(items[start : start + 500])
    with t("load clip list (cold)"):
        clips = project.clips()
    with t("load clip list (cached)"):
        project.clips()
    with t("media rows (lists and filters)"):
        rows = project.media_rows()
    with t("filter: name contains 'CAMA'"):
        hits_name = [r for r in rows if "CAMA" in r["name"]]
    with t("filter: 08:30-10:00 on day 1"):
        lo, hi = "2026-06-14T08:30", "2026-06-14T10:00"
        hits_time = [r for r in rows if r["creation_time"] and lo <= r["creation_time"] < hi]
    with t("filter (SQL, index): creation time range"):
        sql_hits = project._query(
            "SELECT id FROM media_file WHERE creation_time >= ? AND creation_time < ?", (lo, hi)
        )  # fmt: skip
    with t("enqueue analyze tasks"):
        project.enqueue("analyze", [{"target": c, "clip_id": c} for c in clip_ids])
    with t("claim + finish all analyze tasks (batches of 64)"):
        while batch := project.claim(["analyze"], 64):
            project.finish_tasks([(task.id, "done", None) for task in batch])
    with t("store audio analysis rows"):
        project.set_audio_analysis(
            [{"clip_id": c, "cache_key": f"k{c}", "rate": 8000, "samples": 1, "status": "done"} for c in clip_ids]
        )
    pairs = [(rng.choice(clip_ids), rng.choice(clip_ids)) for _ in range(n_matches)]
    with t("enqueue match tasks"):
        project.enqueue(
            "match", [{"target": f"{a}:{b}:{k}", "clip_id": a, "other_clip_id": b} for k, (a, b) in enumerate(pairs)]
        )
    with t("task counts (queue panel)"):
        project.task_counts()
    run_id = project.start_run({})
    matches = [(f"key{k}", _match(a, b, rng), "fingerprint") for k, (a, b) in enumerate(pairs)]
    with t("store matches (batches of 1000)"):
        for start in range(0, len(matches), 1000):
            project.save_matches(run_id, matches[start : start + 1000])
    project.finish_run(run_id, "completed")
    with t("load run match summaries"):
        project.run_match_summaries(run_id)
    with t("load run matches (full detail)"):
        project.run_matches(run_id)
    with t("matches of one clip (index)"):
        project.clip_matches(run_id, clip_ids[len(clip_ids) // 2])
    with t("store AI-analysis rows"):
        with project._tx() as c:
            c.executemany(
                "INSERT INTO ai_analysis (clip_id, analysis_type, result_json, confidence, status, created_at) "
                "VALUES (?, 'scene', '{}', ?, 'done', '2026-01-01')",
                [(rng.choice(clip_ids), rng.random()) for _ in range(n_matches)],
            )
    segments = []
    for k in range(n_transcript):
        text = " ".join(rng.choice(WORDS) for _ in range(12))
        segments.append((clip_ids[k % len(clip_ids)], k * 2.0, k * 2.0 + 2.0, f"S{k % 4}", "en", text, 0.9))
    with t("store transcript segments (batches of 5000)"):
        for start in range(0, len(segments), 5000):
            project.add_transcript_segments(segments[start : start + 5000])
    with t("transcript search (full text)"):
        found = project.search_transcripts("ceremony vows", limit=200)
    with t("store markers"):
        project.add_markers(
            [(rng.choice(clip_ids), rng.uniform(0, 600), rng.choice(("applause", "speech", "clap")), None, 0.5)
             for _ in range(n_markers)]
        )  # fmt: skip
    with t("markers of one type"):
        project.markers(marker_type="applause")
    with t("assign 1000 clips to a camera"):
        device = project.create_device("Camera Z")
        project.set_clips_device(clip_ids[:1000], device)
    with t("database stats"):
        stats = project.database_stats()
    project.close()
    # Reopening checks every file on disk (these paths do not exist, so each one is reported offline).
    with t("reopen (migrate check + stat every file)"):
        project = Project.open(path)
    with t("load clip list after reopen"):
        again = project.clips()
    offline = project.refresh_media_status()
    project.close()
    size = path.stat().st_size
    return {
        "media": n_media,
        "video": n_video,
        "audio": n_audio,
        "matches": n_matches,
        "ai_analysis_rows": n_matches,
        "transcript_segments": n_transcript,
        "markers": n_markers,
        "timings_s": t.results,
        "checks": {
            "clips": len(clips),
            "clips_after_reopen": len(again),
            "name_filter_hits": len(hits_name),
            "time_filter_hits": len(hits_time),
            "time_filter_sql_hits": len(sql_hits),
            "transcript_hits": len(found),
            "status_after_reopen": offline,
        },
        "rows": stats["rows"],
        "db_bytes": size,
        "peak_rss_bytes": peak_rss_bytes(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--media", type=int, nargs="+", default=[5000, 10000], help="media files per run (5% audio)")
    ap.add_argument("--matches", type=int, default=50_000)
    ap.add_argument("--transcript", type=int, default=100_000)
    ap.add_argument("--markers", type=int, default=50_000)
    ap.add_argument("--out", help="write the JSON report here")
    args = ap.parse_args(argv)
    reports = []
    with tempfile.TemporaryDirectory(prefix="syncora-dbbench-") as tmp:
        for n in args.media:
            n_audio = max(1, n // 20)
            report = run(n - n_audio, n_audio, n_matches=args.matches, n_transcript=args.transcript,
                         n_markers=args.markers, directory=Path(tmp))  # fmt: skip
            reports.append(report)
            print(json.dumps(report, indent=2), file=sys.stderr)
    text = json.dumps({"python": sys.version.split()[0], "runs": reports}, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0


__all__ = ["main", "run", "synthetic_items"]

if __name__ == "__main__":
    raise SystemExit(main())
