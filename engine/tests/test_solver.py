"""Global placement from hand-made measurements (no audio involved)."""

from __future__ import annotations

import numpy as np
import pytest

from mcsync.sync import (
    ClipInput,
    ClockReading,
    ClockSource,
    EdgeKind,
    EdgeStatus,
    Flag,
    ManualCorrections,
    ManualOffset,
    MatchStatus,
    OffsetEstimate,
    PairwiseMatch,
    PlacementMethod,
    PlacementStatus,
    SolverParams,
    solve_placements,
)
from mcsync.sync.solver import audio_edge_sigma, unwrap_midnight


def clip(cid, duration=100.0, clock=None, device=None, **kwargs):
    return ClipInput(cid, duration_s=duration, clock=clock, device_id=device, **kwargs)


def match(ref, tgt, offset, confidence=1.0, std_error=1e-4, drift_ppm=0.0, drift_std_ppm=float("inf"), time=0.0):
    status = MatchStatus.CONFIDENT if confidence >= 0.7 else MatchStatus.UNCERTAIN
    est = OffsetEstimate(
        offset_s=offset,
        confidence=confidence,
        status=status,
        offset_time_s=time,
        std_error_s=std_error,
        drift_ppm=drift_ppm,
        drift_std_ppm=drift_std_ppm,
    )
    return PairwiseMatch(ref_id=ref, tgt_id=tgt, offset_s=offset, estimate=est, offset_time_s=time)


def measured(ref, tgt, x, r, t):
    """What a pairwise match reports for clocks ``T_i(τ) = x_i + (1 + r_i)·τ``, at target time ``t``."""
    instant = x[tgt] + (1 + r[tgt]) * t
    offset = (instant - x[ref]) / (1 + r[ref]) - t
    slope = (1 + r[tgt]) / (1 + r[ref]) - 1
    return match(ref, tgt, offset, drift_ppm=-slope * 1e6, drift_std_ppm=0.05, time=t)


TRUTH = {"R": 0.0, "A": 10.0, "B": 25.5, "C": 47.25, "D": 80.0}


def mesh(truth=TRUTH, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    ids = list(truth)
    return [
        match(a, b, truth[b] - truth[a] + rng.normal(0.0, noise) if noise else truth[b] - truth[a])
        for i, a in enumerate(ids)
        for b in ids[i + 1 :]
    ]


def starts(result):
    return {cid: p.start_s for cid, p in result.placements.items()}


def test_exact_mesh_is_reproduced():
    result = solve_placements([clip(c) for c in TRUTH], mesh(), reference_id="R")
    assert starts(result) == pytest.approx(TRUTH, abs=1e-9)
    assert all(p.status == PlacementStatus.SYNCED for p in result.placements.values())
    assert result.placements["R"].method == PlacementMethod.REFERENCE
    assert result.placements["A"].method == PlacementMethod.AUDIO


def test_least_squares_averages_noisy_measurements():
    result = solve_placements([clip(c) for c in TRUTH], mesh(noise=0.0003, seed=1), reference_id="R")
    errors = [abs(result.placements[c].start_s - TRUTH[c]) for c in TRUTH]
    assert max(errors) < 0.0005
    assert all(e.status == EdgeStatus.ACCEPTED for e in result.edges)


def test_positions_are_relative_to_the_reference():
    result = solve_placements([clip(c) for c in TRUTH], mesh(), reference_id="B")
    assert result.placements["B"].start_s == 0.0
    assert result.placements["R"].start_s == pytest.approx(-25.5)
    assert result.timeline() == pytest.approx(TRUTH)


def test_wrong_edge_in_a_cycle_is_rejected():
    matches = mesh()
    matches[4] = match(matches[4].ref_id, matches[4].tgt_id, matches[4].offset_s + 2.0)  # A -> B wrong by 2 s
    result = solve_placements([clip(c) for c in TRUTH], matches, reference_id="R")
    assert starts(result) == pytest.approx(TRUTH, abs=1e-6)
    rejected = [e for e in result.edges if e.status == EdgeStatus.REJECTED]
    assert [(e.node_a, e.node_b, e.reason) for e in rejected] == [("A", "B", Flag.REJECTED_INCONSISTENT)]
    assert rejected[0].residual_s == pytest.approx(-2.0, abs=1e-6)
    assert Flag.CONFLICTING_MATCHES in result.placements["A"].flags
    assert result.placements["A"].status == PlacementStatus.NEEDS_REVIEW


def test_low_confidence_edges_are_ignored():
    matches = [match("R", "A", 10.0), match("R", "B", 99.0, confidence=0.2)]
    result = solve_placements([clip("R"), clip("A"), clip("B")], matches, reference_id="R")
    assert result.placements["B"].status == PlacementStatus.UNSYNCED
    assert result.placements["B"].start_s is None
    ignored = [e for e in result.edges if e.status == EdgeStatus.IGNORED]
    assert [(e.node_b, e.reason) for e in ignored] == [("B", Flag.BELOW_THRESHOLD)]


def test_uncertain_edge_places_clip_for_review():
    matches = [match("R", "A", 10.0, confidence=0.5)]
    result = solve_placements([clip("R"), clip("A")], matches, reference_id="R")
    placement = result.placements["A"]
    assert placement.start_s == pytest.approx(10.0)
    assert placement.status == PlacementStatus.NEEDS_REVIEW
    assert placement.confidence == 0.5


def test_uncertain_match_attaches_one_camera_but_never_merges_two_groups():
    # Two sessions, each a recorder R and camera C matched confidently, plus a camera W whose audio is poor.
    clips = [clip("R1", device="r1"), clip("C1", device="c1"), clip("R2", device="r2"), clip("C2", device="c2"),
             clip("W1", 20.0, device="w"), clip("W2", 20.0, device="w")]  # fmt: skip
    matches = [
        match("R1", "C1", 10.0),
        match("R2", "C2", 30.0),
        match("R1", "W1", 40.0, confidence=0.5),  # W joins session 1 on uncertain evidence: allowed
        match("C1", "W2", 50.0, confidence=0.5),
        match("C1", "C2", 5.0, confidence=0.55),  # would join the two sessions: needs a confident match
    ]
    result = solve_placements(clips, matches, reference_id="R1")
    p = result.placements
    assert p["W1"].start_s == pytest.approx(40.0) and p["W2"].start_s == pytest.approx(60.0)
    assert p["W1"].status == PlacementStatus.NEEDS_REVIEW
    assert p["C2"].group != p["R1"].group
    merged = [e for e in result.edges if e.reason == Flag.UNCERTAIN_MERGE]
    assert [(e.node_a, e.node_b) for e in merged] == [("C1", "C2")]


def test_disconnected_clips_form_detached_groups():
    clips = [clip("R"), clip("A"), clip("X", 300.0), clip("Y"), clip("Z")]
    matches = [match("R", "A", 5.0), match("X", "Y", 12.0)]
    result = solve_placements(clips, matches, reference_id="R")
    assert result.placements["A"].group == 0
    x, y = result.placements["X"], result.placements["Y"]
    assert (x.group, y.group) == (1, 1)
    assert (x.start_s, y.start_s) == (0.0, 12.0)  # anchored at X, the longest clip of the group
    assert Flag.DETACHED_GROUP in y.flags
    assert y.status == PlacementStatus.SYNCED  # a separate session, synced within itself
    strict = solve_placements(clips, matches, reference_id="R", params=SolverParams(detached_groups_need_review=True))
    assert strict.placements["Y"].status == PlacementStatus.NEEDS_REVIEW
    z = result.placements["Z"]
    assert (z.status, z.group, z.start_s) == (PlacementStatus.UNSYNCED, None, None)


def test_manual_offset_overrides_audio_and_rejects_contradicting_edges():
    matches = [match("R", "A", 10.0), match("R", "B", 25.5), match("A", "B", 15.5)]
    corrections = ManualCorrections(offsets=[ManualOffset("B", "R", 30.0)])
    result = solve_placements([clip("R"), clip("A"), clip("B")], matches, reference_id="R", corrections=corrections)
    b = result.placements["B"]
    assert b.start_s == pytest.approx(30.0)
    assert b.method == PlacementMethod.MANUAL
    assert b.confidence == 1.0
    assert Flag.MANUAL in b.flags
    assert result.placements["A"].start_s == pytest.approx(10.0)
    rejected = {(e.node_a, e.node_b) for e in result.edges if e.status == EdgeStatus.REJECTED}
    assert rejected == {("R", "B"), ("A", "B")}


def test_manual_offset_attaches_a_detached_group():
    clips = [clip("R"), clip("A"), clip("X"), clip("Y")]
    matches = [match("R", "A", 5.0), match("X", "Y", 12.0)]
    corrections = ManualCorrections(offsets=[ManualOffset("X", "A", 50.0)])
    result = solve_placements(clips, matches, reference_id="R", corrections=corrections)
    assert result.placements["X"].start_s == pytest.approx(55.0)
    assert result.placements["Y"].start_s == pytest.approx(67.0)
    assert {p.group for p in result.placements.values()} == {0}
    assert result.placements["Y"].method == PlacementMethod.AUDIO


def test_contradicting_manual_offsets_produce_a_warning():
    corrections = ManualCorrections(
        offsets=[ManualOffset("A", "R", 10.0), ManualOffset("B", "A", 5.0), ManualOffset("B", "R", 20.0)]
    )
    result = solve_placements([clip("R"), clip("A"), clip("B")], [], reference_id="R", corrections=corrections)
    assert result.placements["B"].start_s == pytest.approx(15.0)
    assert len(result.warnings) == 1
    manual = [e for e in result.edges if e.kind == EdgeKind.MANUAL]
    assert [e.status for e in manual] == [EdgeStatus.ACCEPTED, EdgeStatus.ACCEPTED, EdgeStatus.REJECTED]
    assert manual[2].reason == Flag.MANUAL_CONFLICT


def test_user_rejected_pair_and_excluded_clip():
    matches = [match("R", "A", 10.0), match("R", "B", 25.5), match("A", "B", 15.5)]
    corrections = ManualCorrections(excluded_clips={"A"})
    corrections.reject_pair("B", "R")
    result = solve_placements([clip("R"), clip("A"), clip("B")], matches, reference_id="R", corrections=corrections)
    assert result.placements["A"].status == PlacementStatus.UNSYNCED
    assert result.placements["A"].flags == (Flag.EXCLUDED,)
    assert result.placements["B"].status == PlacementStatus.UNSYNCED
    reasons = {(e.node_a, e.node_b): e.reason for e in result.edges}
    assert reasons[("R", "B")] == Flag.USER_REJECTED
    assert reasons[("R", "A")] == Flag.EXCLUDED


def test_device_clock_places_an_interrupted_clip_without_audio_match():
    """Camera A's second clip has no usable audio; its camera clock places it."""

    def ct(t):  # camera A's clock runs 3 min 12 s off the reference, but consistently
        return ClockReading(36000.0 + t + 192.0, domain="camA", source=ClockSource.CREATION_TIME)

    clips = [clip("R", 600.0), clip("A1", clock=ct(40.0)), clip("A2", clock=ct(260.0)), clip("A3", clock=ct(400.0))]
    matches = [match("R", "A1", 40.3), match("R", "A3", 400.3)]  # A2 truly starts at 260.3
    result = solve_placements(clips, matches, reference_id="R")
    a2 = result.placements["A2"]
    assert a2.method == PlacementMethod.METADATA
    assert a2.start_s == pytest.approx(260.3, abs=0.01)
    assert a2.status == PlacementStatus.NEEDS_REVIEW  # creation time is only accurate to ~1 s
    assert result.placements["A1"].method == PlacementMethod.AUDIO
    assert result.placements["A1"].start_s == pytest.approx(40.3, abs=1e-6)


def test_shared_timecode_places_clips_and_audio_refines_them():
    tc = lambda t: ClockReading(3600.0 + t, domain="jam", source=ClockSource.TIMECODE)  # noqa: E731
    clips = [clip("R", clock=tc(0.0)), clip("A", clock=tc(10.0)), clip("drone", clock=tc(30.0))]
    matches = [match("R", "A", 10.012)]  # audio sees what timecode rounds to a frame
    result = solve_placements(clips, matches, reference_id="R")
    assert result.placements["A"].start_s == pytest.approx(10.012, abs=1e-5)
    drone = result.placements["drone"]
    assert drone.method == PlacementMethod.TIMECODE
    assert drone.start_s == pytest.approx(30.0, abs=0.02)
    assert drone.status == PlacementStatus.SYNCED


def test_timecode_that_disagrees_with_audio_is_rejected():
    tc = lambda t: ClockReading(3600.0 + t, domain="jam", source=ClockSource.TIMECODE)  # noqa: E731
    clips = [clip("R", clock=tc(0.0)), clip("A", clock=tc(10.0)), clip("B", clock=tc(55.0))]
    matches = [match("R", "A", 10.0), match("R", "B", 20.0), match("A", "B", 10.0)]  # B's timecode is 35 s off
    result = solve_placements(clips, matches, reference_id="R")
    assert result.placements["B"].start_s == pytest.approx(20.0, abs=1e-6)
    assert Flag.TIMECODE_DISAGREES in result.placements["B"].flags
    assert result.placements["B"].status == PlacementStatus.NEEDS_REVIEW
    clock_edges = {e.node_b: e for e in result.edges if e.kind == EdgeKind.CLOCK}
    assert clock_edges["B"].status == EdgeStatus.REJECTED
    assert clock_edges["A"].status == EdgeStatus.ACCEPTED


def test_audio_or_clock_can_be_switched_off():
    tc = lambda t: ClockReading(t, domain="jam")  # noqa: E731
    clips = [clip("R", clock=tc(0.0)), clip("A", clock=tc(10.04))]
    matches = [match("R", "A", 10.0)]
    audio_only = solve_placements(clips, matches, reference_id="R", use_clock=False)
    clock_only = solve_placements(clips, matches, reference_id="R", use_audio=False)
    assert audio_only.placements["A"].start_s == pytest.approx(10.0)
    assert clock_only.placements["A"].start_s == pytest.approx(10.04)
    assert clock_only.placements["A"].method == PlacementMethod.TIMECODE


def test_timecode_crossing_midnight():
    tc = lambda t: ClockReading(t, domain="jam")  # noqa: E731
    clips = [clip("R", clock=tc(86400.0 - 30.0)), clip("A", clock=tc(15.0))]  # 23:59:30 and 00:00:15
    result = solve_placements(clips, [], reference_id="R")
    assert result.placements["A"].start_s == pytest.approx(45.0)
    assert unwrap_midnight([86000.0, 100.0]) == [86000.0, 86500.0]
    assert unwrap_midnight([100.0, 200.0]) == [100.0, 200.0]


def test_edge_uncertainty_grows_as_confidence_falls():
    params = SolverParams()
    assert audio_edge_sigma(match("R", "A", 1.0), params) == pytest.approx(params.min_audio_sigma_s)
    assert audio_edge_sigma(match("R", "A", 1.0, std_error=0.002), params) == pytest.approx(0.002)
    assert audio_edge_sigma(match("R", "A", 1.0, confidence=0.5), params) == pytest.approx(0.001)


# Two cameras with drifting clocks over an hour-long reference: A runs 20 ppm
# fast, B 15 ppm slow. Constant offsets measured mid-overlap disagree by tens
# of ms around cycles; the rate model makes them consistent.
DRIFT_X = {"R": 0.0, "A": 300.0, "B": 1500.0, "C": 2200.0}
DRIFT_R = {"R": 0.0, "A": -20e-6, "B": 15e-6, "C": 0.0}
DRIFT_D = {"R": 3600.0, "A": 2400.0, "B": 1800.0, "C": 600.0}


def drift_matches():
    return [
        measured("R", "A", DRIFT_X, DRIFT_R, 1200.0),
        measured("R", "B", DRIFT_X, DRIFT_R, 900.0),
        measured("R", "C", DRIFT_X, DRIFT_R, 300.0),
        measured("A", "B", DRIFT_X, DRIFT_R, 600.0),  # overlap 1500-2700 on the timeline
        measured("B", "C", DRIFT_X, DRIFT_R, 300.0),
        measured("A", "C", DRIFT_X, DRIFT_R, 250.0),
    ]


def test_clock_drift_model_keeps_cycles_consistent():
    clips = [clip(c, DRIFT_D[c]) for c in DRIFT_X]
    result = solve_placements(clips, drift_matches(), reference_id="R")
    assert all(e.status == EdgeStatus.ACCEPTED for e in result.edges)
    for c in DRIFT_X:
        p = result.placements[c]
        assert p.status == PlacementStatus.SYNCED
        # Best constant placement: true start plus half the drift accumulated over the clip.
        assert p.start_s == pytest.approx(DRIFT_X[c] + DRIFT_R[c] * DRIFT_D[c] / 2, abs=1e-5), c
        assert p.drift_ppm == pytest.approx(-DRIFT_R[c] * 1e6, abs=0.01), c


def test_without_the_drift_model_the_same_data_would_conflict():
    """Sanity check of the scenario: constant offsets alone break a cycle by more
    than the outlier floor, so a drift-unaware solver would reject a correct match."""
    m = {(x.ref_id, x.tgt_id): x.offset_s for x in drift_matches()}
    cycle_error = m[("R", "A")] + m[("A", "B")] - m[("R", "B")]
    assert abs(cycle_error) > SolverParams().outlier_min_s


def test_manual_offset_is_applied_to_displayed_starts_under_drift():
    clips = [clip(c, DRIFT_D[c]) for c in DRIFT_X]
    corrections = ManualCorrections(offsets=[ManualOffset("C", "A", 1900.0)])
    result = solve_placements(clips, drift_matches(), reference_id="R", corrections=corrections)
    shown = {c: p.start_s for c, p in result.placements.items()}
    assert shown["C"] - shown["A"] == pytest.approx(1900.0, abs=1e-9)


def test_unknown_reference_or_manual_clip_is_an_error():
    with pytest.raises(ValueError):
        solve_placements([clip("R")], [], reference_id="nope")
    with pytest.raises(ValueError):
        solve_placements(
            [clip("R")], [], reference_id="R", corrections=ManualCorrections(offsets=[ManualOffset("X", "R", 1.0)])
        )


def test_chapter_continuation_places_a_silent_chapter_exactly():
    """GoPro splits one recording into chapters with no gap between them."""
    chapter = lambda offset: ClockReading(offset, domain="chapter:gopro:0042", source=ClockSource.CHAPTER)  # noqa: E731
    clips = [
        clip("R", 900.0),
        clip("GH010042", 300.0, clock=chapter(0.0)),
        clip("GH020042", 250.0, clock=chapter(300.0)),
    ]
    result = solve_placements(clips, [match("R", "GH010042", 50.0)], reference_id="R")
    second = result.placements["GH020042"]
    assert second.start_s == pytest.approx(350.0, abs=1e-6)
    assert second.method == PlacementMethod.CHAPTER
    assert second.status == PlacementStatus.SYNCED


def test_a_clip_can_carry_several_clocks():
    """Timecode is jam-synced across cameras; each camera also has its own creation-time clock."""

    def clocks(tc, created):
        return dict(
            clock=ClockReading(tc, domain="tc:wall"),
            extra_clocks=(ClockReading(created, domain="ct:camA", source=ClockSource.CREATION_TIME),),
        )

    clips = [
        clip("R", 900.0, clock=ClockReading(36000.0, domain="tc:wall")),
        clip("A1", **clocks(36100.0, 1.78e9)),
        clip("A2", **clocks(36400.0, 1.78e9 + 300.0)),
    ]
    result = solve_placements(clips, [], reference_id="R")
    assert result.placements["A2"].start_s == pytest.approx(400.0, abs=1e-6)
    assert result.placements["A2"].method == PlacementMethod.TIMECODE  # the most trusted clock wins
    with pytest.raises(ValueError):
        ClipInput("bad", duration_s=1.0, clock=ClockReading(0.0, "x"), extra_clocks=(ClockReading(1.0, "x"),))


def test_epoch_creation_times_keep_full_precision():
    ct = lambda t: ClockReading(1_781_000_000.0 + t, domain="ct:camA", source=ClockSource.CREATION_TIME)  # noqa: E731
    clips = [clip("R", 3600.0), clip("A1", clock=ct(0.0)), clip("A2", clock=ct(1234.5678))]
    result = solve_placements(clips, [match("R", "A1", 100.0)], reference_id="R")
    assert result.placements["A2"].start_s == pytest.approx(1334.5678, abs=1e-7)


def test_clock_starts_unwraps_time_of_day_only():
    from mcsync.sync.solver import clock_starts

    tod = [clip(c, clock=ClockReading(v, "tc")) for c, v in (("a", 86000.0), ("b", 300.0))]
    starts = clock_starts(tod)
    assert starts[("b", tod[1].clock)] - starts[("a", tod[0].clock)] == pytest.approx(700.0)
    epoch = [
        clip(c, clock=ClockReading(v, "ct", source=ClockSource.CREATION_TIME))
        for c, v in (("a", 1e9), ("b", 1e9 + 5e4))
    ]
    starts = clock_starts(epoch)
    assert starts[("b", epoch[1].clock)] - starts[("a", epoch[0].clock)] == pytest.approx(5e4)
