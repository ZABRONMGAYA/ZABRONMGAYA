"""High-level synchronisation API.

Synchronisation runs in two phases with very different costs:

* :meth:`SyncEngine.analyze`: pairwise audio matching. Expensive (seconds
  to minutes); its results are persisted and only recomputed when media or
  analysis parameters change.
* :meth:`SyncEngine.solve`: global placement from the stored matches, clocks
  and manual corrections. Takes milliseconds, so the UI re-runs it after
  every manual edit.

:meth:`SyncEngine.run` does both.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from .pairwise import estimate_offset
from .params import DEFAULT_PARAMS, DEFAULT_SOLVER_PARAMS, SolverParams, SyncParams
from .solver import solve_placements, unwrap_midnight
from .types import (
    ClipInput,
    ClockSource,
    Flag,
    ManualCorrections,
    MatchStatus,
    OffsetEstimate,
    PairwiseMatch,
    SyncMode,
    SyncResult,
)

ProgressCallback = Callable[[float, str], None]


class CancelToken(Protocol):
    def is_set(self) -> bool: ...


class SyncCancelled(Exception):
    """Raised when the cancel token is set during analysis."""


@dataclass(frozen=True)
class SyncOptions:
    mode: SyncMode = SyncMode.HYBRID
    #: Clip every other clip is placed relative to. Default: longest clip with audio.
    reference_clip_id: str | None = None
    params: SyncParams = DEFAULT_PARAMS
    solver: SolverParams = field(default=DEFAULT_SOLVER_PARAMS)
    #: Clips from one device cannot overlap, so their pair is never matched.
    skip_same_device_pairs: bool = True
    #: Hybrid mode: extra half-width of the audio search window around the
    #: offset predicted by two clocks of the same domain.
    clock_search_margin_s: float = 2.0
    #: Hybrid mode: if nothing confident is found inside the clock window,
    #: search every lag and flag ``clock_mismatch`` when found elsewhere.
    fallback_full_search: bool = True


@dataclass(frozen=True)
class _Pair:
    ref: ClipInput
    tgt: ClipInput
    #: Clip-level window for ``start(tgt) - start(ref)``.
    window: tuple[float, float] | None


class SyncEngine:
    def __init__(self, options: SyncOptions | None = None) -> None:
        self.options = options or SyncOptions()

    # ------------------------------------------------------------------ API

    def run(
        self,
        clips: Sequence[ClipInput],
        corrections: ManualCorrections | None = None,
        *,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> SyncResult:
        matches = self.analyze(clips, corrections, progress=progress, cancel=cancel)
        return self.solve(clips, matches, corrections)

    def analyze(
        self,
        clips: Sequence[ClipInput],
        corrections: ManualCorrections | None = None,
        *,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
    ) -> list[PairwiseMatch]:
        """Match every pair of clips that could overlap."""
        self._validate(clips)
        if self.options.mode == SyncMode.TIMECODE:
            return []
        excluded = corrections.excluded_clips if corrections else set()
        pairs = self.candidate_pairs([c for c in clips if c.clip_id not in excluded])
        matches: list[PairwiseMatch] = []
        for k, pair in enumerate(pairs):
            if cancel is not None and cancel.is_set():
                raise SyncCancelled()
            if progress is not None:
                progress(k / len(pairs), f"Matching {pair.tgt.clip_id} against {pair.ref.clip_id}")
            matches.append(self.match_pair(pair.ref, pair.tgt, window=pair.window))
        if progress is not None:
            progress(1.0, f"Matched {len(pairs)} pairs")
        return matches

    def solve(
        self,
        clips: Sequence[ClipInput],
        matches: Sequence[PairwiseMatch],
        corrections: ManualCorrections | None = None,
    ) -> SyncResult:
        self._validate(clips)
        mode = self.options.mode
        excluded = corrections.excluded_clips if corrections else set()
        return solve_placements(
            clips,
            matches,
            reference_id=self.reference_id(clips, excluded),
            corrections=corrections,
            use_audio=mode != SyncMode.TIMECODE,
            use_clock=mode != SyncMode.AUDIO,
            params=self.options.solver,
        )

    def match_pair(
        self,
        ref: ClipInput,
        tgt: ClipInput,
        *,
        window: tuple[float, float] | None = None,
    ) -> PairwiseMatch:
        """Match two clips; ``window`` restricts ``start(tgt) - start(ref)``.

        Also used by the UI to "snap" a roughly dragged clip: pass a narrow
        window around the dragged position.
        """
        if ref.audio is None or tgt.audio is None:
            raise ValueError("both clips need audio to be matched")
        # Fine windows are laid over the target, so make the shorter clip the target.
        swap = ref.audio.duration_s < tgt.audio.duration_s
        a, b = (tgt, ref) if swap else (ref, tgt)
        clip_window = window if not swap or window is None else (-window[1], -window[0])
        # audio offset = clip offset + audio_start(b) - audio_start(a)
        shift = b.audio_start_s - a.audio_start_s
        audio_window = None if clip_window is None else (clip_window[0] + shift, clip_window[1] + shift)

        params = self.options.params
        estimate = estimate_offset(a.audio, b.audio, params, search=audio_window)  # type: ignore[arg-type]
        flags: tuple[Flag, ...] = ()
        if audio_window is not None and self.options.fallback_full_search and estimate.status != MatchStatus.CONFIDENT:
            unrestricted = estimate_offset(a.audio, b.audio, params)  # type: ignore[arg-type]
            if unrestricted.status == MatchStatus.CONFIDENT:
                estimate, flags = unrestricted, (Flag.CLOCK_MISMATCH,)

        # Clip-level offset and the target clip's local time at which it holds.
        offset = None if estimate.offset_s is None else estimate.offset_s - shift
        offset_time = estimate.offset_time_s + b.audio_start_s
        if swap:
            estimate = _reverse(estimate)
            if offset is not None:
                offset_time, offset = offset_time + offset, -offset
        return PairwiseMatch(
            ref_id=ref.clip_id,
            tgt_id=tgt.clip_id,
            offset_s=offset,
            estimate=estimate,
            offset_time_s=offset_time,
            audio_shift_s=tgt.audio_start_s - ref.audio_start_s,
            search_window=window,
            flags=flags,
        )

    def candidate_pairs(self, clips: Sequence[ClipInput]) -> list[_Pair]:
        """Pairs worth matching, with clock-derived search windows (hybrid mode)."""
        opts = self.options
        use_clock = opts.mode == SyncMode.HYBRID
        clock_start = _unwrapped_clock_starts(clips) if use_clock else {}
        audio_clips = [c for c in clips if c.audio is not None]
        pairs: list[_Pair] = []
        for i, ref in enumerate(audio_clips):
            for tgt in audio_clips[i + 1 :]:
                if opts.skip_same_device_pairs and ref.device_id is not None and ref.device_id == tgt.device_id:
                    continue
                window = None
                if (
                    ref.clock is not None
                    and tgt.clock is not None
                    and ref.clock.domain == tgt.clock.domain
                    and ref.clip_id in clock_start
                    and tgt.clip_id in clock_start
                ):
                    predicted = clock_start[tgt.clip_id] - clock_start[ref.clip_id]
                    margin = opts.clock_search_margin_s + 3.0 * (
                        _clock_sigma(ref, opts.solver) + _clock_sigma(tgt, opts.solver)
                    )
                    # The clocks say these clips cannot overlap: skip the pair.
                    if predicted > ref.duration_s + margin or predicted + tgt.duration_s < -margin:  # type: ignore[operator]
                        continue
                    window = (predicted - margin, predicted + margin)
                pairs.append(_Pair(ref, tgt, window))
        return pairs

    def reference_id(self, clips: Sequence[ClipInput], excluded: set[str] | frozenset[str] = frozenset()) -> str:
        """The configured reference, else the longest non-excluded clip with audio."""
        if self.options.reference_clip_id is not None:
            if self.options.reference_clip_id in excluded:
                raise ValueError("the reference clip cannot be excluded from synchronisation")
            return self.options.reference_clip_id
        candidates = [c for c in clips if c.clip_id not in excluded] or list(clips)
        pool = [c for c in candidates if c.audio is not None] or candidates
        return max(pool, key=lambda c: (c.duration_s, c.clip_id)).clip_id

    # -------------------------------------------------------------- helpers

    def _validate(self, clips: Sequence[ClipInput]) -> None:
        if not clips:
            raise ValueError("no clips to synchronise")
        ids = [c.clip_id for c in clips]
        if len(set(ids)) != len(ids):
            raise ValueError("clip ids must be unique")
        ref = self.options.reference_clip_id
        if ref is not None and ref not in ids:
            raise ValueError(f"reference clip {ref!r} is not among the clips")
        rate = self.options.params.analysis_rate
        for c in clips:
            if c.audio is not None and c.audio.rate != rate:
                raise ValueError(f"clip {c.clip_id!r} audio is at {c.audio.rate} Hz, expected {rate} Hz")


def _clock_sigma(clip: ClipInput, solver: SolverParams) -> float:
    clock = clip.clock
    assert clock is not None
    if clock.sigma_s is not None:
        return clock.sigma_s
    if clock.source == ClockSource.CREATION_TIME:
        return solver.creation_time_sigma_s
    return solver.bwf_sigma_s if clock.source == ClockSource.BWF else solver.timecode_sigma_s


def _unwrapped_clock_starts(clips: Sequence[ClipInput]) -> dict[str, float]:
    by_domain: dict[str, list[ClipInput]] = {}
    for c in clips:
        if c.clock is not None:
            by_domain.setdefault(c.clock.domain, []).append(c)
    out: dict[str, float] = {}
    for members in by_domain.values():
        starts = unwrap_midnight([c.clock.start_s for c in members])  # type: ignore[union-attr]
        out.update({c.clip_id: s for c, s in zip(members, starts, strict=True)})
    return out


def _reverse(est: OffsetEstimate) -> OffsetEstimate:
    """Express an estimate from the other clip's point of view (to first order in drift)."""
    if est.offset_s is None:
        return est
    return replace(
        est,
        offset_s=-est.offset_s,
        offset_time_s=est.offset_time_s + est.offset_s,
        drift_ppm=-est.drift_ppm if est.drift_ppm else 0.0,
        windows=tuple(replace(w, time_s=w.time_s + w.lag_s, lag_s=-w.lag_s) for w in est.windows),
        alternatives=tuple(replace(a, offset_s=-a.offset_s) for a in est.alternatives),
    )
