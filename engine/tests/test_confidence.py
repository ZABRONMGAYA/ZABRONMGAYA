import pytest

from mcsync.sync import MatchStatus, SyncParams
from mcsync.sync.confidence import AMBIGUOUS_CAP, classify, match_confidence, smoothstep

P = SyncParams()


def conf(**kw):
    base = dict(coarse_psr=30.0, n_inliers=12, inlier_fraction=1.0, overlap_s=120.0, ambiguous=False, params=P)
    base.update(kw)
    return match_confidence(**base)


def test_perfect_evidence_is_fully_confident():
    assert conf() == 1.0


def test_verified_windows_dominate():
    assert conf(n_inliers=1, inlier_fraction=1.0) < P.uncertain_threshold * 2
    assert conf(n_inliers=2, inlier_fraction=1.0) < P.confident_threshold
    assert conf(n_inliers=3, inlier_fraction=1.0) == 1.0
    assert conf(n_inliers=0, inlier_fraction=0.0) == 0.0


def test_partial_agreement_is_not_confident():
    """Same-tempo songs line up about half of their windows by chance."""
    assert conf(n_inliers=5, inlier_fraction=5 / 9) < P.confident_threshold
    assert conf(n_inliers=9, inlier_fraction=9 / 12) >= P.confident_threshold


def test_monotonic_in_each_piece_of_evidence():
    assert conf(coarse_psr=5.0) < conf(coarse_psr=8.0) < conf(coarse_psr=12.0)
    assert conf(inlier_fraction=0.5) < conf(inlier_fraction=0.7) < conf(inlier_fraction=0.9)
    assert conf(overlap_s=3.0) < conf(overlap_s=6.0) < conf(overlap_s=12.0)


def test_weak_detection_still_confident_when_fully_verified():
    assert conf(coarse_psr=P.detection_psr) >= P.confident_threshold


def test_ambiguity_caps_confidence():
    assert conf(ambiguous=True) == AMBIGUOUS_CAP
    assert classify(AMBIGUOUS_CAP, P) == MatchStatus.UNCERTAIN


def test_classify_thresholds():
    assert classify(0.95, P) == MatchStatus.CONFIDENT
    assert classify(P.confident_threshold, P) == MatchStatus.CONFIDENT
    assert classify(0.5, P) == MatchStatus.UNCERTAIN
    assert classify(P.uncertain_threshold - 1e-9, P) == MatchStatus.NO_MATCH


def test_smoothstep():
    assert smoothstep(-1.0, 0.0, 1.0) == 0.0
    assert smoothstep(0.5, 0.0, 1.0) == pytest.approx(0.5)
    assert smoothstep(2.0, 0.0, 1.0) == 1.0
