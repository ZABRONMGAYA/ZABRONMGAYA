"""Audio and timecode synchronisation of multicamera recordings.

Typical use::

    from mcsync.sync import ClipInput, SyncEngine, prepare_signal

    clips = [
        ClipInput("recorder", audio=prepare_signal(pcm_a, 48000), device_id="zoom"),
        ClipInput("camA_001", audio=prepare_signal(pcm_b, 48000), device_id="camA"),
    ]
    result = SyncEngine().run(clips)
    result.placements["camA_001"].start_s   # seconds after the reference clip starts
"""

from .engine import CancelToken, SyncCancelled, SyncEngine, SyncOptions
from .pairwise import estimate_offset
from .params import DEFAULT_PARAMS, DEFAULT_SOLVER_PARAMS, SolverParams, SyncParams
from .signal import AnalysisSignal, prepare_signal
from .solver import solve_placements
from .types import (
    Candidate,
    ClipInput,
    ClipPlacement,
    ClockReading,
    ClockSource,
    EdgeKind,
    EdgeReport,
    EdgeStatus,
    Flag,
    ManualCorrections,
    ManualOffset,
    MatchStatus,
    OffsetEstimate,
    PairwiseMatch,
    PlacementMethod,
    PlacementStatus,
    SyncMode,
    SyncResult,
    WindowMeasurement,
)

__all__ = [
    "DEFAULT_PARAMS",
    "DEFAULT_SOLVER_PARAMS",
    "AnalysisSignal",
    "Candidate",
    "CancelToken",
    "ClipInput",
    "ClipPlacement",
    "ClockReading",
    "ClockSource",
    "EdgeKind",
    "EdgeReport",
    "EdgeStatus",
    "Flag",
    "ManualCorrections",
    "ManualOffset",
    "MatchStatus",
    "OffsetEstimate",
    "PairwiseMatch",
    "PlacementMethod",
    "PlacementStatus",
    "SolverParams",
    "SyncCancelled",
    "SyncEngine",
    "SyncMode",
    "SyncOptions",
    "SyncParams",
    "SyncResult",
    "WindowMeasurement",
    "estimate_offset",
    "prepare_signal",
    "solve_placements",
]
