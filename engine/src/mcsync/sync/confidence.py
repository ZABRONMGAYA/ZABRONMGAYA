"""Confidence scoring and classification of pairwise matches.

The score approximates "how likely is this offset to be right", built from
three independent pieces of evidence:

* **Verification** (dominant): the fine stage measures the lag in several
  windows spread across the overlap. A wrong lag gives lags scattered over the
  whole search range; a right one gives lags on a straight line (constant
  offset plus clock drift) to within a millisecond. Three or more agreeing
  windows are strong evidence; one window proves little.
* **Detection**: how far the coarse peak stands above the correlogram's noise
  (robust z-score, "PSR").
* **Overlap**: short overlaps carry less evidence.

A match whose runner-up candidate is verified almost as well is *ambiguous*
(repeated music, the same song played twice) and is capped below the
confident threshold whatever the other terms say.
"""

from __future__ import annotations

import numpy as np

from .params import SyncParams
from .types import MatchStatus

AMBIGUOUS_CAP = 0.5
_COUNT_FACTOR = {0: 0.0, 1: 0.5, 2: 0.65}


def smoothstep(x: float, lo: float, hi: float) -> float:
    t = float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))
    return t * t * (3.0 - 2.0 * t)


def match_confidence(
    *,
    coarse_psr: float,
    n_inliers: int,
    inlier_fraction: float,
    overlap_s: float,
    ambiguous: bool,
    params: SyncParams,
) -> float:
    # Partial agreement is weak evidence: two unrelated songs at the same tempo
    # can line up their beat grids in ~half of the windows. Real matches agree
    # in nearly all non-silent windows.
    verification = _COUNT_FACTOR.get(n_inliers, 1.0) * (0.3 + 0.7 * smoothstep(inlier_fraction, 0.4, 0.9))
    detection = smoothstep(coarse_psr, params.detection_psr, 2.0 * params.detection_psr)
    overlap = smoothstep(overlap_s, params.min_overlap_s, params.short_overlap_s)
    score = verification * (0.7 + 0.3 * detection) * (0.8 + 0.2 * overlap)
    if ambiguous:
        score = min(score, AMBIGUOUS_CAP)
    return float(np.clip(score, 0.0, 1.0))


def classify(confidence: float, params: SyncParams) -> MatchStatus:
    if confidence >= params.confident_threshold:
        return MatchStatus.CONFIDENT
    if confidence >= params.uncertain_threshold:
        return MatchStatus.UNCERTAIN
    return MatchStatus.NO_MATCH
