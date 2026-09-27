"""A large multi-session production with known ground truth, as real media files.

Each *session* (a ceremony, a talk, a concert set) is one synthetic scene. Every session is covered by sound
recorders (Broadcast WAV, split into consecutive files with continuous time references) and by cameras whose clips
start and stop throughout it. Cameras differ the way real ones do: clock errors of minutes to hours, noise, room
sound, phone-like band limits, clock drift, low levels, overloaded preamps. Problem files are mixed in: muted clips,
clips without audio, damaged files, byte-identical copies, clips from an unrelated event, and sidecar files.

``generate_production(root, ProductionPlan())`` writes 4,000 camera clips and 200 recorder files (plus the problem
files) and returns the truth: for every file, its session, the scene time it starts at, and what a correct
synchroniser should conclude about it.

    python -m mcsync.testing.production OUT_DIR [--sessions 20 --cameras 8 --clips-per-camera 25 ...]

writes the same production from the command line (the one SCALABILITY_TEST_REPORT.md was measured on, with the
defaults) for :mod:`mcsync.testing.stress`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .media import SHOOT_DATE, run_ffmpeg
from .synthetic import Scene, make_scene, record

SCENE_RATE = 16000
SCENE_KINDS = ("speech", "mixed", "music", "speech", "mixed")
#: Camera audio conditions, rotated over each session's cameras.
CAMERA_CONDITIONS: tuple[tuple[str, dict], ...] = (
    ("clean", {"snr_db": 25}),
    ("noisy", {"snr_db": 5}),
    ("far", {"snr_db": 12, "reverb_rt60_s": 0.8, "direct_to_reverb_db": -3}),
    ("phone", {"snr_db": 15, "highpass_hz": 400, "lowpass_hz": 3000}),
    ("drift", {"snr_db": 20, "clock_ppm": 35.0}),
    ("quiet", {"snr_db": 15, "gain_db": -30}),
    ("distorted", {"snr_db": 20, "clip": 0.1}),
    ("scratch", {"snr_db": 8, "reverb_rt60_s": 1.0, "direct_to_reverb_db": -6, "highpass_hz": 300}),
)


@dataclass(frozen=True)
class ProductionPlan:
    sessions: int = 20
    cameras: int = 8  # per session
    clips_per_camera: int = 25
    recorders: int = 2  # per session
    files_per_recorder: int = 5
    recorder_file_s: float = 300.0
    clip_s: tuple[float, float] = (20.0, 60.0)
    problems: bool = True
    seed: int = 7

    @property
    def session_s(self) -> float:
        return self.files_per_recorder * self.recorder_file_s

    @property
    def video_files(self) -> int:
        return self.sessions * self.cameras * self.clips_per_camera

    @property
    def audio_files(self) -> int:
        return self.sessions * self.recorders * self.files_per_recorder


@dataclass
class FileTruth:
    session: int | None
    start: float | None  # scene time of the first sample
    device: str
    kind: str  # video | audio | other
    condition: str
    expect: str  # sync | no-audio | silent | unrelated | damaged | duplicate
    copy_of: str | None = None  # duplicates: the file they copy


@dataclass
class Production:
    root: Path
    plan: ProductionPlan
    files: dict[str, FileTruth] = field(default_factory=dict)

    def save(self) -> Path:
        path = self.root / "truth.json"
        path.write_text(json.dumps({"plan": asdict(self.plan), "files": {k: asdict(v) for k, v in self.files.items()}}))
        return path

    @classmethod
    def load(cls, root: Path) -> Production:
        data = json.loads((root / "truth.json").read_text())
        plan = ProductionPlan(**{**data["plan"], "clip_s": tuple(data["plan"]["clip_s"])})
        return cls(root, plan, {k: FileTruth(**v) for k, v in data["files"].items()})


# ---------------------------------------------------------------------------
# Workers (separate processes; scenes are shared as memory-mapped files)
# ---------------------------------------------------------------------------


def _render_scene(path: str, kind: str, duration_s: float, seed: int) -> str:
    scene = make_scene(duration_s, kind=kind, rate=SCENE_RATE, seed=seed)
    np.save(path, scene.samples)
    return path


_SCENES: dict[str, Scene] = {}


def _scene(path: str) -> Scene:
    scene = _SCENES.get(path)
    if scene is None:
        _SCENES.clear()
        scene = _SCENES[path] = Scene(np.load(path, mmap_mode="r"), SCENE_RATE)
    return scene


def _write_file(job: dict) -> str:
    out = Path(job["path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    settings = dict(job.get("settings", {}))
    clip = settings.pop("clip", None)
    pcm = None
    if job.get("scene"):
        pcm = record(_scene(job["scene"]), start_s=job["start"], duration_s=job["duration"], rate=job["rate"],
                     seed=zlib.crc32(out.name.encode()) % 100_000, **settings)  # fmt: skip
        if clip is not None:
            peak = float(np.max(np.abs(pcm))) or 1.0
            pcm = np.clip(pcm, -clip * peak, clip * peak)
    if job["type"] == "wav":
        run_ffmpeg([
            "-f", "f32le", "-ar", str(job["rate"]), "-ac", "1", "-i", "pipe:0", "-c:a", "pcm_s16le",
            "-write_bext", "1", "-metadata", f"time_reference={job['time_reference']}",
            "-metadata", "originator=ZOOM", "-metadata", "originator_reference=F6", str(out),
        ], pcm)  # fmt: skip
    else:
        args = ["-f", "lavfi", "-i", f"color=c=0x383535:size=64x36:rate=25:duration={job['duration']}"]
        if pcm is not None:
            args += ["-f", "f32le", "-ar", str(job["rate"]), "-ac", "1", "-i", "pipe:0"]
        args += ["-map", "0:v"] + (["-map", "1:a"] if pcm is not None else [])
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-g", "250"]
        if pcm is not None:
            args += ["-c:a", "aac", "-b:a", "48k"]
        if job.get("timecode"):
            args += ["-timecode", job["timecode"]]
        args += ["-metadata", f"creation_time={job['created']}", str(out)]
        run_ffmpeg(args, pcm)
    return str(out)


# ---------------------------------------------------------------------------
# Plan → files
# ---------------------------------------------------------------------------


def _iso(seconds_after_shoot: float) -> str:
    import datetime as dt

    return (SHOOT_DATE + dt.timedelta(seconds=int(seconds_after_shoot))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timecode(seconds: float, fps: int = 25) -> str:
    seconds %= 86400
    frames = int(round(seconds * fps))
    return f"{frames // (3600 * fps):02d}:{frames // (60 * fps) % 60:02d}:{frames // fps % 60:02d}:{frames % fps:02d}"


def generate_production(
    root: Path, plan: ProductionPlan | None = None, *, workers: int | None = None, progress=None
) -> Production:
    """Write the production under ``root`` (skipping files that already exist) and return its truth."""
    plan = plan or ProductionPlan()
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(plan.seed)
    prod = Production(root, plan)
    scenes_dir = root.parent / f".{root.name}-scenes"
    scenes_dir.mkdir(exist_ok=True)
    jobs: list[dict] = []
    scene_jobs: list[tuple[str, str, float, int]] = []
    day_gap = 3 * 3600.0  # sessions are hours apart
    for s in range(plan.sessions):
        kind = SCENE_KINDS[s % len(SCENE_KINDS)]
        scene_path = str(scenes_dir / f"session{s:02d}-{kind}.npy")
        scene_jobs.append((scene_path, kind, plan.session_s + 30.0, plan.seed * 1000 + s))
        wall = s * day_gap  # scene time 0 of this session, in seconds after SHOOT_DATE
        for r in range(plan.recorders):
            device = f"S{s:02d}_REC{r + 1}"
            for k in range(plan.files_per_recorder):
                start = k * plan.recorder_file_s
                rel = f"SOUND/{device}/{device}_{k + 1:03d}.WAV"
                prod.files[rel] = FileTruth(s, start, device, "audio", "recorder", "sync")
                jobs.append({"type": "wav", "path": str(root / rel), "scene": scene_path, "start": start,
                             "duration": plan.recorder_file_s, "rate": 16000,
                             "time_reference": int(round((36000 + wall + start) * 16000)) % (86400 * 16000),
                             "settings": {"snr_db": 35}})  # fmt: skip
        for c in range(plan.cameras):
            cond_name, cond = CAMERA_CONDITIONS[(c + s) % len(CAMERA_CONDITIONS)]
            device = f"S{s:02d}_CAM{chr(65 + c)}"
            clock_error = float(rng.choice([0.0, 97.0, -3600.0, 7200.0, float(rng.uniform(-600, 600))]))
            free_run_tc = c % 3 == 0
            lengths = rng.uniform(*plan.clip_s, plan.clips_per_camera)
            spare = max(plan.session_s - lengths.sum() - 5.0, 0.0)
            gaps = rng.dirichlet(np.ones(plan.clips_per_camera + 1)) * spare
            t = gaps[0]
            base = 36000 + wall + clock_error  # the camera's clock at scene time 0
            for k in range(plan.clips_per_camera):
                rel = f"CARDS/{device}/DCIM/C{k + 1:04d}.MP4"
                start = float(t)
                if free_run_tc:  # a camera starts recording on a frame of its own timecode
                    start = round((base + start) * 25) / 25 - base
                prod.files[rel] = FileTruth(s, start, device, "video", cond_name, "sync")
                tc = _timecode(base + start) if free_run_tc else None
                jobs.append({"type": "mp4", "path": str(root / rel), "scene": scene_path, "start": start,
                             "duration": float(lengths[k]), "rate": 48000, "settings": cond,
                             "created": _iso(wall + start + clock_error), "timecode": tc})  # fmt: skip
                t += lengths[k] + gaps[k + 1]
    if plan.problems:
        _add_problems(prod, jobs, scene_jobs, rng)

    with ProcessPoolExecutor(workers or os.cpu_count() or 2) as pool:
        missing_scenes = [j for j in scene_jobs if not Path(j[0]).is_file()]
        list(pool.map(_render_scene, *zip(*missing_scenes, strict=True))) if missing_scenes else None
        todo = [j for j in jobs if not Path(j["path"]).is_file()]
        for n, _ in enumerate(pool.map(_write_file, todo, chunksize=8), 1):
            if progress is not None and n % 100 == 0:
                progress(n, len(todo))
    _finish_problems(prod)
    prod.save()
    return prod


def _add_problems(prod: Production, jobs: list[dict], scene_jobs: list, rng: np.random.Generator) -> None:
    plan, root = prod.plan, prod.root
    session0 = scene_jobs[0][0]
    for k in range(10):  # microphone off: audio stream present, silent
        rel = f"CARDS/PROBLEMS/MUTED/M{k + 1:04d}.MP4"
        start = float(rng.uniform(0, plan.session_s - 40))
        prod.files[rel] = FileTruth(0, start, "PROBLEMS_MUTED", "video", "muted", "silent")
        jobs.append({"type": "mp4", "path": str(root / rel), "scene": session0, "start": start, "duration": 30.0,
                     "rate": 48000, "settings": {"gain_db": -140, "snr_db": None}, "created": _iso(start)})  # fmt: skip
    for k in range(10):  # no audio stream at all (drone, gimbal)
        rel = f"CARDS/PROBLEMS/DRONE/DJI_{k + 1:04d}.MP4"
        prod.files[rel] = FileTruth(None, None, "PROBLEMS_DRONE", "video", "no-audio", "no-audio")
        jobs.append({"type": "mp4", "path": str(root / rel), "scene": None, "duration": 25.0, "rate": 48000,
                     "created": _iso(float(k * 100))})  # fmt: skip
    other = str(Path(session0).with_name("unrelated-event.npy"))
    scene_jobs.append((other, "mixed", 400.0, 424242))
    for k in range(5):  # footage from another event that ended up on the drive
        rel = f"CARDS/PROBLEMS/OTHER_EVENT/X{k + 1:04d}.MP4"
        prod.files[rel] = FileTruth(None, None, "PROBLEMS_OTHER", "video", "unrelated", "unrelated")
        jobs.append({"type": "mp4", "path": str(root / rel), "scene": other, "start": float(k * 70), "duration": 40.0,
                     "rate": 48000, "settings": {"snr_db": 20}, "created": _iso(float(k * 70))})  # fmt: skip


def _finish_problems(prod: Production) -> None:
    """Damaged files and copies are made from files already written."""
    if not prod.plan.problems:
        return
    root = prod.root
    cams = sorted(rel for rel, f in prod.files.items() if f.kind == "video" and f.expect == "sync")
    for k, rel in enumerate(cams[:5]):  # truncated in the middle of the header (card pulled out while writing)
        bad = f"CARDS/PROBLEMS/DAMAGED/D{k + 1:04d}.MP4"
        dst = root / bad
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            dst.write_bytes((root / rel).read_bytes()[:1500])
        prod.files[bad] = FileTruth(None, None, "PROBLEMS_DAMAGED", "video", "damaged", "damaged")
    for rel in cams[5:10]:  # the same file copied to a backup folder
        copy = f"BACKUP/{Path(rel).parent.parent.name}_{Path(rel).name}"
        dst = root / copy
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(root / rel, dst)
        orig = prod.files[rel]
        prod.files[copy] = FileTruth(orig.session, orig.start, orig.device, "video", orig.condition, "duplicate", rel)
    for rel in ("CARDS/README.txt", "CARDS/S00_CAMA/DCIM/C0001.THM", "CARDS/S00_CAMA/MEDIAPRO.XML"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("sidecar")
        prod.files[rel] = FileTruth(None, None, "", "other", "sidecar", "ignored")


# ---------------------------------------------------------------------------
# Scoring a synchronisation against the truth
# ---------------------------------------------------------------------------


def evaluate(prod: Production, index: dict, *, tolerance_s: float = 0.02) -> dict:
    """Score ``media.index`` output against the truth.

    Within each session, every file's position relative to the session's first recorder file is compared with the
    truth. ``exact``: within ``tolerance_s`` (half a frame at 25 fps); ``wrong``: placed elsewhere; ``unplaced``: left
    for manual sync; ``split``: placed, but in another sync group than its session's recorder.
    """
    cols = {c: k for k, c in enumerate(index["columns"])}
    rows = {}
    for r in index["rows"]:
        rel = os.path.relpath(r[cols["path"]], prod.root).replace(os.sep, "/")
        rows[rel] = {c: r[k] for c, k in cols.items()}
    anchors: dict[int, str] = {}
    for rel, f in sorted(prod.files.items()):
        if f.kind == "audio" and f.session is not None:
            anchors.setdefault(f.session, rel)
    twins = {f.copy_of: rel for rel, f in prod.files.items() if f.copy_of}
    outcome: dict[str, dict[str, int]] = {}
    errors: list[float] = []
    wrong: list[dict] = []
    groups_per_session: dict[int, set] = {}
    for rel, f in prod.files.items():
        if f.kind == "other":
            continue
        row = rows.get(rel)
        bucket = outcome.setdefault(f.expect if f.expect != "sync" else f"sync:{f.condition}", {})
        if row is None:
            key = "failed" if f.expect == "damaged" else "not imported"  # damaged files never become clips
            bucket[key] = bucket.get(key, 0) + 1
            continue
        if f.expect == "duplicate" or rel in twins:
            # Byte-identical copies: either one may be the one left out; the other must be synchronised.
            copy_rel, orig_rel = (rel, f.copy_of) if f.expect == "duplicate" else (twins[rel], rel)
            a, b = rows.get(orig_rel), rows.get(copy_rel)
            one_skipped = (
                a is not None
                and b is not None
                and sorted((a["category"] == "skipped", b["category"] == "skipped")) == [False, True]
            )
            kept = (b if a is not None and a["category"] == "skipped" else a) if one_skipped else None
            if f.expect == "duplicate":
                bucket["skipped" if one_skipped else "both kept"] = (
                    bucket.get("skipped" if one_skipped else "both kept", 0) + 1
                )
                continue
            if kept is not None and kept is not row:
                row = kept
        if f.expect != "sync":
            key = row["category"]
            if f.expect in ("silent", "no-audio", "unrelated") and row["method"] == "audio" and key != "review":
                key = "placed by audio (wrong)"  # a suggestion left for review is not a wrong sync
            bucket[key] = bucket.get(key, 0) + 1
            continue
        anchor = rows.get(anchors[f.session])
        truth = f.start - prod.files[anchors[f.session]].start
        if row["start_s"] is None or anchor is None or anchor["start_s"] is None:
            key = "unplaced"
        elif row["group"] != anchor["group"]:
            key = "split"
        else:
            err = (row["start_s"] - anchor["start_s"]) - truth
            errors.append(abs(err))
            key = "exact" if abs(err) <= tolerance_s else "wrong"
            if key == "wrong":
                wrong.append({"file": rel, "error_s": err, "confidence": row["confidence"], "method": row["method"]})
            groups_per_session.setdefault(f.session, set()).add(row["group"])
        bucket[key] = bucket.get(key, 0) + 1
    errs = np.array(errors) if errors else np.zeros(1)
    totals: dict[str, int] = {}
    for name, bucket in outcome.items():
        if name.startswith("sync:"):
            for k, v in bucket.items():
                totals[k] = totals.get(k, 0) + v
    return {
        "sync_files": {k: v for k, v in sorted(totals.items())},
        "by_condition": dict(sorted(outcome.items())),
        "error_ms": {
            "max": float(errs.max() * 1000),
            "p99": float(np.percentile(errs, 99) * 1000),
            "median": float(np.median(errs) * 1000),
        },  # fmt: skip
        "sessions_one_group": sum(len(g) == 1 for g in groups_per_session.values()),
        "sessions": prod.plan.sessions,
        "wrong": wrong[:50],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write a generated production with known truth.")
    ap.add_argument("out", type=Path, help="production folder (files that already exist are kept)")
    defaults = ProductionPlan()
    ap.add_argument("--sessions", type=int, default=defaults.sessions)
    ap.add_argument("--cameras", type=int, default=defaults.cameras, help="per session")
    ap.add_argument("--clips-per-camera", type=int, default=defaults.clips_per_camera)
    ap.add_argument("--recorders", type=int, default=defaults.recorders, help="per session")
    ap.add_argument("--files-per-recorder", type=int, default=defaults.files_per_recorder)
    ap.add_argument("--seed", type=int, default=defaults.seed)
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args(argv)
    plan = ProductionPlan(
        sessions=args.sessions,
        cameras=args.cameras,
        clips_per_camera=args.clips_per_camera,
        recorders=args.recorders,
        files_per_recorder=args.files_per_recorder,
        seed=args.seed,
    )
    t0 = time.monotonic()

    def progress(done: int, total: int) -> None:
        print(f"{done}/{total} files, {time.monotonic() - t0:.0f} s", file=sys.stderr, flush=True)

    prod = generate_production(args.out, plan, workers=args.workers, progress=progress)
    print(f"{len(prod.files)} files in {time.monotonic() - t0:.0f} s: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
