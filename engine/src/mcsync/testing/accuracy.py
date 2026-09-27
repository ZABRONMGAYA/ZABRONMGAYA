"""Accuracy benchmark: known offsets under difficult audio, through both matching paths.

A clean reference recording (a sound recorder) and a camera recording that starts ``offset`` seconds later are
rendered from one synthetic scene, for every condition × offset. Each pair is synchronised twice:

* **full**: the complete matcher over every lag (what small projects use for every pair);
* **staged**: what large productions use: the camera clip is looked up in a fingerprint index that also holds
  unrelated recordings, the candidate offsets are verified in narrow windows, and a clip without a candidate
  gets the extended (full) search.

``python -m mcsync.testing.accuracy`` prints a JSON report; SCALABILITY_TEST_REPORT.md records the results.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from mcsync.sync.engine import SyncEngine, verify_pair
from mcsync.sync.landmarks import IndexClip, LandmarkIndex, compute_landmarks
from mcsync.sync.params import DEFAULT_PARAMS
from mcsync.sync.signal import prepare_signal
from mcsync.sync.types import ClipInput, MatchStatus

from .synthetic import SCENE_RMS, Scene, ambience, make_scene, music_like, record, speech_like

SCENE_RATE = 16000
OFFSETS = (0.5, 1.0, 3.0, 10.0, 60.0)
REFERENCE_S = 120.0
CLIP_S = 30.0

#: Condition → (scene kind, camera recording settings).
CONDITIONS: dict[str, tuple[str, dict]] = {
    "clean": ("speech", {"snr_db": 40}),
    "noisy": ("speech", {"snr_db": 0}),
    "speech": ("speech", {"snr_db": 20, "reverb_rt60_s": 0.4, "direct_to_reverb_db": 3}),
    "music": ("music", {"snr_db": 20}),
    "applause": ("applause", {"snr_db": 20}),
    "crowd": ("crowd", {"snr_db": 10}),
    "scratch": (
        "mixed",
        {
            "snr_db": 6,
            "reverb_rt60_s": 1.0,
            "direct_to_reverb_db": -6,
            "highpass_hz": 300,
            "lowpass_hz": 3000,
            "gain_db": -20,
        },
    ),  # fmt: skip
    "distorted": ("mixed", {"snr_db": 20, "clip": 0.08}),
    "missing": ("mixed", {"silent": True}),
}


def _normalise(x: np.ndarray) -> Scene:
    x = x - x.mean()
    x *= SCENE_RMS / (np.std(x) + 1e-12)
    return Scene(x.astype(np.float32), SCENE_RATE)


def scene_for(kind: str, duration_s: float, seed: int) -> Scene:
    rng = np.random.default_rng(seed)
    if kind == "applause":  # dense clapping over room tone
        return _normalise(ambience(duration_s, SCENE_RATE, rng, events_per_min=600))
    if kind == "crowd":  # many overlapping talkers, laughter and glasses
        talkers = sum(speech_like(duration_s, SCENE_RATE, rng) for _ in range(6))
        return _normalise(talkers + 2.0 * ambience(duration_s, SCENE_RATE, rng, events_per_min=30))
    if kind == "music":
        return _normalise(music_like(duration_s, SCENE_RATE, rng))
    return make_scene(duration_s, kind=kind, rate=SCENE_RATE, seed=seed)


def camera(scene: Scene, offset: float, settings: dict, seed: int) -> np.ndarray:
    settings = dict(settings)
    clip = settings.pop("clip", None)
    if settings.pop("silent", False):
        return np.zeros(int(CLIP_S * 8000), dtype=np.float32)
    x = record(scene, start_s=offset, duration_s=CLIP_S, rate=8000, seed=seed, **settings)
    if clip is not None:  # overloaded preamp: hard clipping at a fraction of the peak
        peak = float(np.max(np.abs(x))) or 1.0
        x = np.clip(x, -clip * peak, clip * peak)
    return x


@dataclass
class Outcome:
    found: bool
    error_ms: float | None
    confidence: float
    status: str
    how: str


def _outcome(match, truth: float, how: str) -> Outcome:  # noqa: ANN001
    ok = match.offset_s is not None and match.status != MatchStatus.NO_MATCH
    err = abs(match.offset_s - truth) * 1000 if match.offset_s is not None else None
    return Outcome(bool(ok and err is not None and err < 20.0), err, float(match.confidence), match.status.value, how)


def run(conditions: list[str] | None = None, offsets: tuple[float, ...] = OFFSETS, directory: Path | None = None,
        distractors: int = 12) -> dict:  # fmt: skip
    engine = SyncEngine()
    params = DEFAULT_PARAMS
    rows = []
    tmp = Path(tempfile.mkdtemp(prefix="syncora-accuracy-", dir=directory))
    # Unrelated recordings share the fingerprint index, as in a real production.
    others = []
    for k in range(distractors):
        sc = scene_for(("speech", "music", "mixed", "applause")[k % 4], 60.0, 9000 + k)
        sig = prepare_signal(record(sc, start_s=0.0, duration_s=55.0, rate=8000, seed=k, snr_db=20), 8000, params)
        path = tmp / f"other{k}.npy"
        np.save(path, compute_landmarks(sig.samples, 8000, params))
        others.append(IndexClip(f"other{k}", str(path), f"dev-other{k}", 1 + (sig.n_samples - 512) // 256))
    for c, name in enumerate(conditions or list(CONDITIONS)):
        kind, settings = CONDITIONS[name]
        scene = scene_for(kind, REFERENCE_S + 10.0, 100 + c)
        ref_sig = prepare_signal(record(scene, start_s=0.0, duration_s=REFERENCE_S, rate=8000, seed=1, snr_db=30),
                                 8000, params)  # fmt: skip
        ref_lm = tmp / f"{name}-ref.npy"
        np.save(ref_lm, compute_landmarks(ref_sig.samples, 8000, params))
        ref = ClipInput("ref", audio=ref_sig, device_id="recorder")
        for offset in offsets:
            tgt_sig = prepare_signal(camera(scene, offset, settings, seed=int(offset * 10) + c), 8000, params)
            tgt = ClipInput("cam", audio=tgt_sig, device_id="camera")
            t0 = time.perf_counter()
            full = _outcome(engine.match_pair(ref, tgt), offset, "full search")
            t_full = time.perf_counter() - t0

            t0 = time.perf_counter()
            tgt_lm = tmp / f"{name}-{offset}.npy"
            np.save(tgt_lm, compute_landmarks(tgt_sig.samples, 8000, params))
            clips = [
                IndexClip("ref", str(ref_lm), "recorder", 1 + (ref_sig.n_samples - 512) // 256),
                IndexClip("cam", str(tgt_lm), "camera", 1 + (tgt_sig.n_samples - 512) // 256),
                *others,
            ]
            index = LandmarkIndex.build(tmp / f"index-{name}-{offset}", clips, 8000)
            cands = [x for x in index.query("cam") if x.other == "ref"]
            wrong = [x for x in index.query("cam") if x.other != "ref"]
            if cands:
                # start(cam) - start(ref) = candidate offset (both audio streams start with their clips)
                windows = [(x.offset_s - 0.5, x.offset_s + 0.5) for x in cands]
                staged = _outcome(verify_pair(engine.options, ref, tgt, windows, "landmark"), offset,
                                  f"fingerprint candidate ({cands[0].votes} votes)")  # fmt: skip
                if staged.status != "confident":
                    staged = _outcome(engine.match_pair(ref, tgt), offset, "extended search (after a weak candidate)")
            else:
                staged = _outcome(engine.match_pair(ref, tgt), offset, "extended search (no candidate)")
            t_staged = time.perf_counter() - t0
            rows.append({
                "condition": name, "offset_s": offset,
                "full": full.__dict__ | {"seconds": round(t_full, 3)},
                "staged": staged.__dict__ | {"seconds": round(t_staged, 3)},
                "false_candidates": len(wrong),
            })  # fmt: skip
            print(f"{name:10s} {offset:5.1f}s full={full.found}/{full.confidence:.2f} "
                  f"staged={staged.found}/{staged.confidence:.2f} ({staged.how})", file=sys.stderr)  # fmt: skip
    return {"reference_s": REFERENCE_S, "clip_s": CLIP_S, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    report = run()
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
