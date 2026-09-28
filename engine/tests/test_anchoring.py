"""Clock anchoring (temporal continuity): a camera's clock, calibrated by its confidently matched clips, points
the narrow searches for the clips it could not match."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mcsync.pipeline.runner import ANCHOR_MIN_UNCERTAINTY_S, anchored_pairs
from mcsync.sync.types import PlacementMethod, PlacementStatus

BASE = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
CLOCK_ERROR_S = 97.0  # the camera's clock runs 97 s ahead


def placement(start, confidence=1.0, method=PlacementMethod.AUDIO, status=PlacementStatus.SYNCED):
    return SimpleNamespace(start_s=start, confidence=confidence, method=method, status=status, group=0)


def row(cid, device, kind, start):
    created = BASE + timedelta(seconds=start + (CLOCK_ERROR_S if kind == "camera" else 0.0))
    info = SimpleNamespace(creation_time=created)
    return SimpleNamespace(engine_id=cid, device_id=device, device_kind=kind, info=info)


def clip(duration):
    return SimpleNamespace(audio=object(), duration_s=duration)


def setup(unmatched_start=700.0):
    rows = [row("1", 1, "recorder", 0.0)] + [row(c, 2, "camera", s) for c, s in (("2", 100), ("3", 300), ("4", 500))]
    rows.append(row("5", 2, "camera", unmatched_start))
    placements = {"1": placement(0.0, method=PlacementMethod.REFERENCE), "2": placement(100.0),
                  "3": placement(300.0), "4": placement(500.0),
                  "5": placement(123.0, confidence=0.3, status=PlacementStatus.NEEDS_REVIEW)}  # fmt: skip
    inputs = {"1": clip(1000.0), "2": clip(30.0), "3": clip(30.0), "4": clip(30.0), "5": clip(12.0)}
    return SimpleNamespace(placements=placements), rows, inputs


def test_an_unmatched_clip_is_searched_where_its_calibrated_clock_puts_it():
    result, rows, inputs = setup()
    pairs = anchored_pairs(result, rows, inputs)
    assert [(p.ref, p.tgt, p.stage) for p in pairs] == [("1", "5", "anchored")]
    ((lo, hi),) = pairs[0].windows
    assert lo < 700.0 < hi  # start(5) - start(recorder) predicted by the camera's clock
    assert hi - lo == pytest.approx(2 * ANCHOR_MIN_UNCERTAINTY_S, abs=0.01)


def test_nothing_is_searched_without_a_calibrated_clock_or_an_overlapping_reference():
    result, rows, inputs = setup()
    for cid in ("2", "3", "4"):  # no confident clip of the camera: its clock is not calibrated
        result.placements[cid] = placement(result.placements[cid].start_s, confidence=0.4)
    assert anchored_pairs(result, rows, inputs) == []
    result, rows, inputs = setup(unmatched_start=5000.0)  # predicted beyond every placed recording
    assert anchored_pairs(result, rows, inputs) == []
