"""Data types shared by the synchronisation engine.

Time conventions (used everywhere in :mod:`mcsync.sync`):

* All times are float seconds. float64 keeps sub-microsecond precision over
  24 hours, far below one audio sample.
* An *offset* between a reference ``R`` and a target ``T`` is the position of
  T's start on R's clock: ``start(T) - start(R)``. Positive means T started
  recording after R.
* ``drift_ppm`` is how fast T's clock runs relative to R's, in parts per
  million. Positive means T's clock runs fast (T records more samples per real
  second than R).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .signal import AnalysisSignal


class MatchStatus(StrEnum):
    CONFIDENT = "confident"
    UNCERTAIN = "uncertain"
    NO_MATCH = "no_match"


class Flag(StrEnum):
    """Machine-readable reasons attached to matches and placements.

    The UI maps each value to an explanation; keep values stable.
    """

    # Pairwise matching
    SILENT = "silent"
    SILENT_OVERLAP = "silent_overlap"
    NO_OVERLAP = "no_overlap"
    NO_CORRELATION = "no_correlation"
    AMBIGUOUS = "ambiguous"
    INCONSISTENT_WINDOWS = "inconsistent_windows"
    UNVERIFIED = "unverified"
    SHORT_OVERLAP = "short_overlap"
    DRIFT = "drift"
    CLOCK_MISMATCH = "clock_mismatch"
    # Global placement
    REJECTED_INCONSISTENT = "rejected_inconsistent"
    USER_REJECTED = "user_rejected"
    BELOW_THRESHOLD = "below_threshold"
    CONFLICTING_MATCHES = "conflicting_matches"
    TIMECODE_DISAGREES = "timecode_disagrees"
    DETACHED_GROUP = "detached_group"
    MANUAL = "manual"
    MANUAL_CONFLICT = "manual_conflict"
    EXCLUDED = "excluded"
    NO_AUDIO = "no_audio"


class ClockSource(StrEnum):
    """Where a clip's absolute start time came from."""

    TIMECODE = "timecode"  # SMPTE timecode track (tmcd, MXF, ...)
    BWF = "bwf"  # Broadcast WAV bext time_reference
    CREATION_TIME = "creation_time"  # container creation date (1 s resolution)
    CHAPTER = "chapter"  # position inside one recording split into files (GoPro chapters, 4 GB splits)

    @property
    def is_time_of_day(self) -> bool:
        """Readings are seconds since midnight and wrap at 24 h."""
        return self in (ClockSource.TIMECODE, ClockSource.BWF)


class PlacementMethod(StrEnum):
    REFERENCE = "reference"
    AUDIO = "audio"
    TIMECODE = "timecode"
    METADATA = "metadata"
    CHAPTER = "chapter"
    MANUAL = "manual"
    NONE = "none"


class PlacementStatus(StrEnum):
    SYNCED = "synced"
    NEEDS_REVIEW = "needs_review"
    UNSYNCED = "unsynced"


class SyncMode(StrEnum):
    AUDIO = "audio"  # waveform matching only
    TIMECODE = "timecode"  # clock readings only
    HYBRID = "hybrid"  # clocks narrow the audio search and fill gaps


# ---------------------------------------------------------------------------
# Pairwise results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowMeasurement:
    """One fine-stage window of a pairwise match."""

    time_s: float  # window centre, seconds from the target's first audio sample
    lag_s: float  # measured offset in that window
    correlation: float  # Pearson correlation of the aligned waveforms
    inlier: bool


@dataclass(frozen=True)
class Candidate:
    """A coarse-stage lag hypothesis and how well the fine stage verified it."""

    offset_s: float
    coarse_psr: float
    n_inliers: int = 0
    inlier_fraction: float = 0.0


@dataclass(frozen=True)
class OffsetEstimate:
    """Result of matching two audio signals (audio start to audio start)."""

    offset_s: float | None
    confidence: float
    status: MatchStatus
    #: Target-local time (seconds from its audio start) at which ``offset_s``
    #: holds exactly: the middle of the overlap. With clock drift the offset
    #: elsewhere is ``offset_s - drift_ppm·1e-6·(t - offset_time_s)``.
    offset_time_s: float = 0.0
    drift_ppm: float = 0.0
    #: Standard error of ``drift_ppm``; inf when it could not be measured.
    drift_std_ppm: float = float("inf")
    std_error_s: float = float("inf")
    overlap_s: float = 0.0
    coarse_psr: float = 0.0
    uniqueness: float = 0.0
    n_windows: int = 0
    inlier_fraction: float = 0.0
    correlation: float = 0.0
    flags: tuple[Flag, ...] = ()
    windows: tuple[WindowMeasurement, ...] = ()
    alternatives: tuple[Candidate, ...] = ()

    @property
    def n_inliers(self) -> int:
        return sum(1 for w in self.windows if w.inlier)


@dataclass(frozen=True)
class PairwiseMatch:
    """A measured offset between two clips (clip start to clip start).

    ``estimate`` is the raw audio measurement: first audio sample to first
    audio sample, oriented ref → tgt. Clip-level values differ from it by
    ``audio_shift_s`` when a container's audio does not start with its video.
    """

    ref_id: str
    tgt_id: str
    #: ``start(tgt) - start(ref)`` in seconds; None when nothing was found.
    #: Under clock drift this holds at ``offset_time_s`` (see OffsetEstimate).
    offset_s: float | None
    estimate: OffsetEstimate
    #: Clip-local time of the target at which ``offset_s`` holds.
    offset_time_s: float = 0.0
    #: ``audio_start(tgt) - audio_start(ref)``: clip offset = audio offset - shift.
    audio_shift_s: float = 0.0
    #: Clip-level search window used, if a clock prior restricted it.
    search_window: tuple[float, float] | None = None
    flags: tuple[Flag, ...] = ()

    @property
    def confidence(self) -> float:
        return self.estimate.confidence

    @property
    def status(self) -> MatchStatus:
        return self.estimate.status

    @property
    def all_flags(self) -> tuple[Flag, ...]:
        return tuple(dict.fromkeys(self.estimate.flags + self.flags))

    @property
    def alternatives(self) -> tuple[Candidate, ...]:
        """Runner-up candidates as clip-level offsets (for "pick another match")."""
        return tuple(replace(a, offset_s=a.offset_s - self.audio_shift_s) for a in self.estimate.alternatives)


# ---------------------------------------------------------------------------
# Engine inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockReading:
    """A clip's start time on some external clock.

    ``domain`` names the clock. Clips share a domain only when their clocks are
    known to agree: all devices jam-synced to one timecode generator share one
    domain; otherwise each device is its own domain (its clips are still
    correctly ordered relative to each other).
    """

    start_s: float
    domain: str
    source: ClockSource = ClockSource.TIMECODE
    sigma_s: float | None = None


@dataclass(eq=False)
class ClipInput:
    """Everything the engine needs to know about one clip."""

    clip_id: str
    audio: AnalysisSignal | None = None
    duration_s: float | None = None
    #: Camera/recorder identity. Clips from one device never overlap in time.
    device_id: str | None = None
    clock: ClockReading | None = None
    #: Further readings of the clip start on other clocks (a clip can carry
    #: timecode, its camera's creation time and a chapter position at once).
    extra_clocks: tuple[ClockReading, ...] = ()
    #: Position of the first audio sample relative to the clip start (from the
    #: container's stream start times). Usually 0; negative if audio starts first.
    audio_start_s: float = 0.0

    def __post_init__(self) -> None:
        if self.duration_s is None:
            if self.audio is None:
                raise ValueError(f"clip {self.clip_id!r}: duration_s is required without audio")
            self.duration_s = self.audio_start_s + self.audio.duration_s
        if self.duration_s <= 0:
            raise ValueError(f"clip {self.clip_id!r}: duration must be positive")
        domains = [c.domain for c in self.clocks]
        if len(set(domains)) != len(domains):
            raise ValueError(f"clip {self.clip_id!r}: two clock readings in the same domain")

    @property
    def clocks(self) -> tuple[ClockReading, ...]:
        return ((self.clock,) if self.clock is not None else ()) + tuple(self.extra_clocks)


@dataclass(frozen=True)
class ManualOffset:
    """User placement: ``start(clip_id) = start(anchor_clip_id) + offset_s``."""

    clip_id: str
    anchor_clip_id: str
    offset_s: float


@dataclass
class ManualCorrections:
    offsets: list[ManualOffset] = field(default_factory=list)
    #: Unordered clip-id pairs whose audio match the user marked as wrong.
    rejected_pairs: set[frozenset[str]] = field(default_factory=set)
    #: Clips the user removed from synchronisation.
    excluded_clips: set[str] = field(default_factory=set)

    def reject_pair(self, a: str, b: str) -> None:
        self.rejected_pairs.add(frozenset((a, b)))

    def is_rejected(self, a: str, b: str) -> bool:
        return frozenset((a, b)) in self.rejected_pairs


# ---------------------------------------------------------------------------
# Engine outputs
# ---------------------------------------------------------------------------


class EdgeKind(StrEnum):
    AUDIO = "audio"
    CLOCK = "clock"
    MANUAL = "manual"


class EdgeStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IGNORED = "ignored"


@dataclass(frozen=True)
class EdgeReport:
    """How one measurement was used by the global solver."""

    kind: EdgeKind
    node_a: str  # clip id, or ``clock:<domain>`` for a clock node
    node_b: str
    offset_s: float | None
    sigma_s: float | None
    confidence: float
    status: EdgeStatus
    reason: Flag | None = None
    residual_s: float | None = None


@dataclass(frozen=True)
class ClipPlacement:
    clip_id: str
    #: Where to place the clip, relative to the group anchor (the reference
    #: clip in group 0). With clock drift this is the best constant placement:
    #: misalignment is ±drift·duration/2, zero in the middle of the clip.
    start_s: float | None
    group: int | None
    method: PlacementMethod
    confidence: float
    status: PlacementStatus
    flags: tuple[Flag, ...] = ()
    #: Clock rate relative to the group anchor; positive runs fast. Playing the
    #: clip at speed ``1 / (1 - drift_ppm·1e-6)`` removes the drift entirely.
    drift_ppm: float = 0.0


@dataclass
class SyncResult:
    reference_id: str
    placements: dict[str, ClipPlacement]
    matches: list[PairwiseMatch]
    edges: list[EdgeReport]
    warnings: list[str] = field(default_factory=list)

    def group_members(self, group: int = 0) -> list[ClipPlacement]:
        members = [p for p in self.placements.values() if p.group == group]
        return sorted(members, key=lambda p: (p.start_s, p.clip_id))

    def timeline(self, group: int = 0) -> dict[str, float]:
        """Clip starts of one group, shifted so the earliest clip starts at 0."""
        members = self.group_members(group)
        if not members:
            return {}
        origin = min(p.start_s for p in members)  # type: ignore[type-var]
        return {p.clip_id: p.start_s - origin for p in members}  # type: ignore[operator]

    def needs_review(self) -> list[str]:
        """Clips that need the editor's attention: uncertain, conflicting or unsynced."""
        return [p.clip_id for p in self.placements.values() if p.status != PlacementStatus.SYNCED]
