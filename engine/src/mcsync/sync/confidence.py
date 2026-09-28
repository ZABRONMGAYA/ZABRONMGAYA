"""Confidence scoring and classification of pairwise matches.

The score approximates "how likely is this offset to be right", built from
three independent pieces of evidence:

* **Verification** (dominant): the fine stage measures the lag in several
  windows spread across the overlap. A wrong lag gives lags scattered over the
  whole search range; a right one gives lags on a straight line (constant
  offset plus clock drift) to within a millisecond. Three or more agreeing
  windows are strong evidence; one window proves little.
* **Detection**: how far the coarse peak stands above the correlogram's noise
  (robust z-score, "PSR"), or, when three or more windows agree, how sharply
  their fine correlation peaks stand out (the same sound, however faint).
* **Overlap**: short overlaps carry less evidence.

A match whose runner-up candidate is verified almost as well is *ambiguous*
(repeated music, the same song played twice) and is capped below the
confident threshold whatever the other terms say. So is a match whose
verification windows line up in time but barely correlate (*weak
correlation*) **and** whose correlation peaks are blunt: the same beat grid,
not the same sound. Across 22,000 verified pairs of a 4,000-clip production,
every correct match correlated at 0.15 or more (reverberant, noisy and
band-limited cameras included); the wrong ones found in looping music
correlated at 0.14 or less, most below 0.05. A gimbal camera whose
microphone mostly hears its motors and wind correlates at only 0.09–0.13
with the recorder even when right, so correlation alone cannot tell: the
peaks can. Same-sound windows peak 25–40 robust z-scores above their
correlogram; beat-grid windows at most 10 (``same_sound_prominence``: 15).
"""

from __future__ import annotations

import numpy as np

from .params import SyncParams
from .types import MatchStatus

AMBIGUOUS_CAP = 0.5
#: Median window correlation below which a match cannot be confident (see the module docstring).
WEAK_CORRELATION = 0.1
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
    correlation: float | None = None,
    prominence: float = 0.0,
) -> float:
    # Partial agreement is weak evidence: two unrelated songs at the same tempo
    # can line up their beat grids in ~half of the windows. Real matches agree
    # in nearly all non-silent windows.
    verification = _COUNT_FACTOR.get(n_inliers, 1.0) * (0.3 + 0.7 * smoothstep(inlier_fraction, 0.4, 0.9))
    detection = smoothstep(coarse_psr, params.detection_psr, 2.0 * params.detection_psr)
    if n_inliers >= 3:
        # Three or more windows hearing the same sound at the same lag (sharp peaks) detect the match as surely as a
        # strong coarse peak does, however faint the sound was in the coarse features.
        sharp = smoothstep(prominence, params.same_sound_prominence, 2 * params.same_sound_prominence)
        detection = max(detection, sharp)
    overlap = smoothstep(overlap_s, params.min_overlap_s, params.short_overlap_s)
    score = verification * (0.7 + 0.3 * detection) * (0.8 + 0.2 * overlap)
    if ambiguous:
        score = min(score, AMBIGUOUS_CAP)
    if correlation is not None and correlation < WEAK_CORRELATION and prominence < params.same_sound_prominence:
        score = min(score, AMBIGUOUS_CAP)
    if coarse_psr < params.detection_psr and prominence < params.same_sound_prominence:
        # Windows that agree are not enough on their own: without a clear coarse peak or a prominent fine peak they
        # can agree on a strong room reflection (a consistent lag a few tens of milliseconds off) or a beat grid.
        # Measured on the 4,238-file production: three such matches were placed 20-34 ms off at 0.70.
        score = min(score, AMBIGUOUS_CAP)
    return float(np.clip(score, 0.0, 1.0))


def classify(confidence: float, params: SyncParams) -> MatchStatus:
    if confidence >= params.confident_threshold:
        return MatchStatus.CONFIDENT
    if confidence >= params.uncertain_threshold:
        return MatchStatus.UNCERTAIN
    return MatchStatus.NO_MATCH
