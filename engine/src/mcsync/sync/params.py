"""Tunable parameters for the synchronisation engine.

Defaults are chosen for speech/music captured by camera microphones and
external recorders (weddings, events, interviews). Every value is documented in
``docs/SYNC_ENGINE.md``; change them only with a matching test.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyncParams:
    """Parameters of the pairwise (two-recording) offset estimator."""

    # --- Analysis signal -------------------------------------------------
    #: Sample rate every recording is converted to before analysis. 8 kHz keeps
    #: speech intelligibility cues while making a 3 h recording ~345 MB float32.
    analysis_rate: int = 8000
    #: Band-pass applied before analysis. Removes wind/handling rumble and the
    #: top octave where camera microphones and recorders differ the most.
    band_low_hz: float = 150.0
    band_high_hz: float = 3500.0
    #: In-band level (dBFS) below which a whole recording is treated as silent.
    silence_floor_dbfs: float = -65.0

    # --- Coarse stage (envelope cross-correlation) ------------------------
    #: Frame rate of the log-energy envelope. 200 Hz = 5 ms resolution.
    feature_rate: int = 200
    #: Soft floor of the log-energy envelope, relative to mean frame energy.
    #: Hides noise-floor differences (camera hiss vs. clean lavalier).
    envelope_floor_db: float = -40.0
    #: Length of the moving average removed from the log envelope.
    envelope_detrend_s: float = 1.0
    #: Minimum overlap for a lag to be considered at all.
    min_overlap_s: float = 3.0
    #: Half-width of the zone around a peak in which no other candidate may lie.
    peak_exclusion_s: float = 0.15
    #: Maximum number of coarse candidates refined by the fine stage.
    max_candidates: int = 3
    #: A candidate is refined only if its PSR is at least this fraction of the best.
    candidate_ratio: float = 0.6
    #: Coarse peak-to-sidelobe ratio (robust z-score) required to attempt a match.
    detection_psr: float = 5.0

    # --- Fine stage (windowed GCC-PHAT on the waveform) -------------------
    fine_window_s: float = 10.0
    min_fine_window_s: float = 1.0
    max_fine_windows: int = 12
    #: Search half-width around the coarse lag, before the drift allowance.
    fine_margin_s: float = 0.03
    #: Largest clock drift between two devices the engine will track.
    max_drift_ppm: float = 100.0
    #: PHAT weighting exponent: 0 = plain cross-correlation, 1 = full PHAT.
    phat_beta: float = 0.8
    #: Windows whose lag is within this distance of the fitted line are inliers.
    inlier_tolerance_s: float = 0.001
    #: Windows quieter than this (dB relative to the clip RMS) are skipped.
    window_silence_db: float = -35.0

    # --- Classification ---------------------------------------------------
    confident_threshold: float = 0.7
    uncertain_threshold: float = 0.35
    #: Overlaps shorter than this are flagged ``short_overlap``.
    short_overlap_s: float = 10.0
    #: Accumulated drift over the overlap above which ``drift`` is flagged.
    drift_warning_s: float = 0.010

    def __post_init__(self) -> None:
        if self.analysis_rate <= 0 or self.feature_rate <= 0:
            raise ValueError("analysis_rate and feature_rate must be positive")
        if self.analysis_rate % self.feature_rate:
            raise ValueError("analysis_rate must be an integer multiple of feature_rate")
        if not 0 < self.band_low_hz < self.band_high_hz < self.analysis_rate / 2:
            raise ValueError("band must satisfy 0 < low < high < analysis_rate/2")
        if not 0.0 <= self.phat_beta <= 1.0:
            raise ValueError("phat_beta must be within [0, 1]")
        if not 0.0 < self.uncertain_threshold < self.confident_threshold <= 1.0:
            raise ValueError("need 0 < uncertain_threshold < confident_threshold <= 1")
        if self.max_candidates < 1 or self.max_fine_windows < 1:
            raise ValueError("max_candidates and max_fine_windows must be >= 1")
        if self.min_fine_window_s > self.fine_window_s:
            raise ValueError("min_fine_window_s must not exceed fine_window_s")

    @property
    def hop(self) -> int:
        """Samples per envelope frame."""
        return self.analysis_rate // self.feature_rate


@dataclass(frozen=True)
class SolverParams:
    """Parameters of the global (multi-clip) placement solver."""

    #: Audio matches below this confidence never become solver edges.
    min_edge_confidence: float = 0.35
    #: Floor for the standard deviation assigned to an audio edge.
    min_audio_sigma_s: float = 0.0005
    #: An edge is rejected when |residual| > max(outlier_sigma * sigma, outlier_min_s).
    outlier_sigma: float = 4.0
    outlier_min_s: float = 0.005
    #: Tolerance when checking manual constraints against each other.
    manual_tolerance_s: float = 1e-6
    #: Default clock uncertainty per clock source when the reading has none.
    timecode_sigma_s: float = 0.04
    bwf_sigma_s: float = 0.02
    creation_time_sigma_s: float = 1.0
    chapter_sigma_s: float = 0.002
    #: Placement confidence reported for clips placed only through a clock.
    timecode_confidence: float = 0.9
    creation_time_confidence: float = 0.3
    chapter_confidence: float = 0.95
    confident_threshold: float = 0.7
    #: Clocks at least this precise tie clips together as firmly as a confident audio match (timecode, sound
    #: recorder time references and chapters; not camera creation times).
    precise_clock_sigma_s: float = 0.1
    #: Uncertain audio matches only attach clips that confident matches, precise clocks and manual placements leave
    #: unconnected. Inside a group those already tie together, an uncertain match adds nothing but risk: repetitive
    #: audio (a music loop, a metronome) offers many plausible wrong offsets.
    uncertain_edges_bridge_only: bool = True
    #: Sync groups other than the reference's are separate sessions in a production; set True when every clip was
    #: expected to join one timeline (then clips in other groups need review).
    detached_groups_need_review: bool = False


DEFAULT_PARAMS = SyncParams()
DEFAULT_SOLVER_PARAMS = SolverParams()
