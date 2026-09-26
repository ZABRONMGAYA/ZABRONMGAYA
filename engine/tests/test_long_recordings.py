"""Hour-long recordings with clock drift (run with ``-m slow``; excluded from quick runs)."""

from __future__ import annotations

import time

import pytest

from mcsync.sync import (
    ClipInput,
    EdgeStatus,
    Flag,
    MatchStatus,
    PlacementStatus,
    SyncEngine,
    SyncOptions,
    estimate_offset,
)
from mcsync.testing.synthetic import capture, make_scene

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def ceremony():
    return make_scene(3800.0, kind="mixed", rate=8000, seed=42)


@pytest.fixture(scope="module")
def recorder(ceremony):
    return capture(ceremony, start_s=60.0, duration_s=3660.0, snr_db=30, seed=1)


def test_one_hour_reference_against_drifting_camera(ceremony, recorder):
    camera = capture(ceremony, start_s=1260.5, duration_s=1800.0, clock_ppm=-25.0, snr_db=5, reverb_rt60_s=0.9, seed=2)
    started = time.perf_counter()
    est = estimate_offset(recorder, camera)
    elapsed = time.perf_counter() - started
    assert est.status == MatchStatus.CONFIDENT
    assert est.drift_ppm == pytest.approx(-25.0, abs=1.0)
    assert Flag.DRIFT in est.flags  # 45 ms over the 30-minute overlap
    assert est.offset_s == pytest.approx(1200.5 + 25e-6 * 900.0, abs=0.5e-3)
    assert elapsed < 10.0, f"pairwise match of 1 h vs 30 min took {elapsed:.1f} s"


def test_long_multicam_session(ceremony, recorder):
    clips = [ClipInput("recorder", audio=recorder, device_id="rec")]
    truth = {"recorder": 60.0}
    for k, (start, duration) in enumerate([(100.0, 1500.0), (1700.25, 1400.0), (3200.0, 550.0)]):
        cid = f"camA_{k}"
        clips.append(
            ClipInput(
                cid,
                audio=capture(ceremony, start_s=start, duration_s=duration, snr_db=8, seed=10 + k),
                device_id="camA",
            )
        )
        truth[cid] = start
    clips.append(
        ClipInput(
            "camB",
            audio=capture(ceremony, start_s=900.0, duration_s=2400.0, snr_db=8, clock_ppm=12.0, seed=20),
            device_id="camB",
        )
    )
    truth["camB"] = 900.0
    result = SyncEngine(SyncOptions(reference_clip_id="recorder")).run(clips)
    assert all(e.status != EdgeStatus.REJECTED for e in result.edges)
    for cid, start in truth.items():
        p = result.placements[cid]
        assert p.status == PlacementStatus.SYNCED, p
        expected, drift = start - 60.0, 0.0
        if cid == "camB":  # 12 ppm fast: best constant placement is centred on the clip
            expected, drift = start - 60.0 - 12e-6 * 2400.0 / 2, 12.0
        assert p.start_s == pytest.approx(expected, abs=0.5e-3), cid
        assert p.drift_ppm == pytest.approx(drift, abs=0.3), cid
