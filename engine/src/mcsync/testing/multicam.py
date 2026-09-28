"""A wedding with known offsets, the way a small crew shoots it, to measure synchronisation where it is hard.

    python -m mcsync.testing.multicam WORK_DIR [--out report.json] [--seed 7]

Devices (every file is cut from one rendered scene, so every true offset is known to the sample):

* ``ZOOM``: the sound recorder, two files back to back (the reference);
* ``LAV``: a lavalier recorder started later, quieter;
* ``FX3``: the main camera, long clips from the back of the room (reverberant), its clock 97 s fast;
* ``GIMBAL``: a Sony camera on a gimbal, dozens of short clips (8 to 40 s) whose microphone mostly hears the
  gimbal's motors, wind and the operator's hands; its clock was never set (2 h 13 min off);
* ``A7IV``: a camera recording quietly (30 dB low);
* ``IPHONE``: a phone, band-limited sound; one clip has no sound at all.

The report gives, per device and overall: how many clips were synchronised (and at which confidence), left for
review, for manual sync or failed; the error of every placed clip against the truth (known-offset accuracy); false
matches (placed with confidence but wrong by more than a frame); processing time, memory and processor use.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from mcsync import resources
from mcsync.service.app import EngineService

from .production import _iso, _write_file
from .synthetic import make_scene

FRAME_S = 0.04  # one frame at 25 fps: the tolerance for a correct placement


@dataclass(frozen=True)
class Device:
    name: str
    kind: str  # recorder | camera | phone
    clips: int
    length_s: tuple[float, float]
    settings: dict
    clock_error_s: float = 0.0


DEVICES = (
    Device("ZOOM", "recorder", 2, (1200.0, 1200.0), {"snr_db": 35}),
    Device("LAV", "recorder", 1, (1800.0, 1800.0), {"snr_db": 30, "gain_db": -12, "lowpass_hz": 6000}),
    Device("FX3", "camera", 6, (240.0, 420.0), {"snr_db": 12, "reverb_rt60_s": 0.8, "direct_to_reverb_db": -3},
           97.0),
    Device("GIMBAL", "camera", 60, (8.0, 40.0), {"snr_db": 6, "gimbal_db": 6, "highpass_hz": 150, "gain_db": -6},
           2 * 3600 + 13 * 60 + 7.4),
    Device("A7IV", "camera", 10, (60.0, 180.0), {"snr_db": 15, "gain_db": -30}, -600.0),
    Device("IPHONE", "phone", 6, (30.0, 90.0), {"snr_db": 15, "highpass_hz": 400, "lowpass_hz": 3000}, 3.0),
)  # fmt: skip
SCENE_S = 2400.0
#: A gimbal whose microphone is worse: the motors and wind louder than the room, far from the speakers, clips of 5 to
#: 25 s, and one clip in five recorded with the microphone muted (nothing but hiss).
HARD_GIMBAL = Device("GIMBAL", "camera", 60, (5.0, 25.0),
                     {"snr_db": 3, "gimbal_db": 12, "highpass_hz": 150, "gain_db": -6, "reverb_rt60_s": 0.6,
                      "direct_to_reverb_db": -6}, 2 * 3600 + 13 * 60 + 7.4)  # fmt: skip


@dataclass
class Truth:
    device: str
    start: float  # scene time of the file's first sample
    duration: float
    has_audio: bool = True


def devices(scenario: str) -> tuple[Device, ...]:
    if scenario == "hard":
        return tuple(HARD_GIMBAL if d.name == "GIMBAL" else d for d in DEVICES)
    return DEVICES


def generate(root: Path, seed: int = 7, scenario: str = "standard") -> dict[str, Truth]:
    """Write the shoot under ``root`` (existing files are kept) and return the truth per relative path."""
    root.mkdir(parents=True, exist_ok=True)
    scene_file = root / ".scene.npy"
    if not scene_file.is_file():
        scene = make_scene(SCENE_S + 30, kind="mixed", rate=16000, seed=seed)
        np.save(scene_file, scene.samples)
    rng = np.random.default_rng(seed)
    truth: dict[str, Truth] = {}
    jobs: list[dict] = []
    for dev in devices(scenario):
        if dev.kind == "recorder":
            start0 = 0.0 if dev.name == "ZOOM" else 300.0
            for k in range(dev.clips):
                start, dur = start0 + k * dev.length_s[0], dev.length_s[0]
                rel = f"SOUND/{dev.name}/{dev.name}_{k + 1:03d}.WAV"
                truth[rel] = Truth(dev.name, start, dur)
                jobs.append({"type": "wav", "path": str(root / rel), "scene": str(scene_file), "start": start,
                             "duration": dur, "rate": 48000, "settings": dev.settings,
                             "time_reference": int(round((36000 + start) * 48000))})  # fmt: skip
            continue
        lengths = rng.uniform(*dev.length_s, dev.clips)
        spare = max(SCENE_S - lengths.sum() - 5.0, 0.0)
        gaps = rng.dirichlet(np.ones(dev.clips + 1)) * spare
        t = float(gaps[0])
        for k in range(dev.clips):
            rel = f"CARDS/{dev.name}/C{k + 1:04d}.MP4"
            silent = dev.name == "IPHONE" and k == 2  # a clip recorded with the microphone off
            muted = scenario == "hard" and dev.name == "GIMBAL" and k % 5 == 4  # sound track, but only hiss
            settings = {"snr_db": -40, "gain_db": -40} if muted else dev.settings
            truth[rel] = Truth(dev.name, t, float(lengths[k]), has_audio=not silent and not muted)
            jobs.append({"type": "mp4", "path": str(root / rel), "scene": None if silent else str(scene_file),
                         "start": t, "duration": float(lengths[k]), "rate": 48000, "settings": settings,
                         "created": _iso(t + dev.clock_error_s)})  # fmt: skip
            t += float(lengths[k] + gaps[k + 1])
    todo = [j for j in jobs if not Path(j["path"]).is_file()]
    with ThreadPoolExecutor(max(1, min(8, os.cpu_count() or 2))) as pool:
        list(pool.map(_write_file, todo))
    (root / "truth.json").write_text(json.dumps({k: asdict(v) for k, v in truth.items()}, indent=1))
    return truth


def evaluate(truth: dict[str, Truth], root: Path, index: dict, threshold: float) -> dict:
    cols = {c: k for k, c in enumerate(index["columns"])}
    rows = {os.path.relpath(r[cols["path"]], root).replace(os.sep, "/"): {c: r[k] for c, k in cols.items()}
            for r in index["rows"]}  # fmt: skip
    anchor_rel = "SOUND/ZOOM/ZOOM_001.WAV"
    anchor = rows[anchor_rel]
    per_device: dict[str, dict] = {}
    errors: list[float] = []
    confident_errors: list[float] = []
    confidences: list[float] = []
    false_matches: list[dict] = []
    for rel, t in sorted(truth.items()):
        row = rows.get(rel)
        d = per_device.setdefault(t.device, {"clips": 0, "categories": {}, "placed": 0, "exact": 0, "wrong": 0,
                                             "confidence": []})  # fmt: skip
        d["clips"] += 1
        cat = row["category"] if row else "not imported"
        d["categories"][cat] = d["categories"].get(cat, 0) + 1
        if row is None or row["start_s"] is None or anchor["start_s"] is None or row["group"] != anchor["group"]:
            continue
        err = (row["start_s"] - anchor["start_s"]) - (t.start - truth[anchor_rel].start)
        d["placed"] += 1
        conf = row["confidence"] or 0.0
        if rel != anchor_rel:
            d["confidence"].append(conf)
            confidences.append(conf)
        ok = abs(err) <= FRAME_S
        d["exact" if ok else "wrong"] += 1
        errors.append(abs(err))
        if cat in ("synchronized", "high_confidence"):
            confident_errors.append(abs(err))
            if not ok:
                false_matches.append({"file": rel, "error_s": round(err, 3), "confidence": conf,
                                      "method": row["method"]})  # fmt: skip
    for d in per_device.values():
        c = d.pop("confidence")
        d["median_confidence"] = round(float(np.median(c)), 3) if c else None
    totals: dict[str, int] = {}
    for d in per_device.values():
        for k, v in d["categories"].items():
            totals[k] = totals.get(k, 0) + v
    errs = np.array(errors) if errors else np.zeros(1)
    auto = totals.get("synchronized", 0) + totals.get("high_confidence", 0)
    return {
        "media": len(truth),
        "categories": totals,
        "automatically_synchronized": auto,
        "average_confidence": round(float(np.mean(confidences)), 3) if confidences else None,
        "median_confidence": round(float(np.median(confidences)), 3) if confidences else None,
        "false_matches": len(false_matches),
        "false_match_rate": round(len(false_matches) / max(1, len(confident_errors)), 4),
        "known_offset_error_ms": {"median": round(float(np.median(errs)) * 1000, 2),
                                  "p95": round(float(np.percentile(errs, 95)) * 1000, 2),
                                  "max": round(float(errs.max()) * 1000, 2),
                                  "within_one_frame": int(np.sum(errs <= FRAME_S)), "placed": len(errors)},
        "per_device": per_device,
        "false_match_list": false_matches[:20],
        "threshold": threshold,
    }  # fmt: skip


def run(root: Path, work: Path, seed: int = 7, scenario: str = "standard") -> dict:
    from .stress import Sampler

    truth = generate(root, seed, scenario)
    work.mkdir(parents=True, exist_ok=True)
    project = work / "Wedding.syncora"
    for p in (project, Path(f"{project}-wal"), Path(f"{project}-shm")):
        p.unlink(missing_ok=True)
    sampler = Sampler()
    sampler.start()
    t0 = time.monotonic()
    svc = EngineService(cache_dir=str(work / "cache"))
    try:
        svc.project_create(str(project), "Wedding")
        svc.media_add([str(root)])

        def wait(predicate, timeout: float) -> None:  # noqa: ANN001
            deadline = time.monotonic() + timeout
            while not predicate():
                if time.monotonic() > deadline:
                    raise TimeoutError
                time.sleep(0.2)

        def busy() -> bool:
            snap = svc.pipeline.snapshot()  # type: ignore[union-attr]
            return bool(snap["running"]) or svc.project.pending_count(["probe", "analyze"]) > 0  # type: ignore[union-attr]

        wait(lambda: not svc.pipeline.import_request(1).walking, 600)  # type: ignore[union-attr]
        wait(lambda: not busy(), 3600)
        t_analysed = time.monotonic() - t0
        run_id = svc.sync_start()["run_id"]
        wait(lambda: (svc.pipeline.sync_state or {}).get("phase") == "done"  # type: ignore[union-attr]
             and svc.pipeline.sync_state["run_id"] == run_id, 3600)  # type: ignore[union-attr]  # fmt: skip
        t_synced = time.monotonic() - t0
        state = svc.pipeline.sync_state or {}  # type: ignore[union-attr]
        index = svc.media_index()
        summary = svc.sync_summary()
        report = evaluate(truth, root, index, summary["threshold"])
        report["candidates"] = {"pairs_planned": state.get("planned"), "extended_searches": state.get("extended"),
                                "strategy": state.get("strategy")}  # fmt: skip
        report["seconds"] = {"analysis": round(t_analysed, 1), "sync": round(t_synced - t_analysed, 1),
                             "total": round(t_synced, 1)}  # fmt: skip
    finally:
        svc.close()
        sampler.stop_event.set()
        sampler.join()
    report["resources"] = sampler.summary()
    report["machine"] = resources.detect().to_dict()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("work", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--scenario", choices=["standard", "hard"], default="standard")
    args = parser.parse_args(argv)
    report = run(args.work / "media", args.work / "run", args.seed, args.scenario)
    report["scenario"] = args.scenario
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
