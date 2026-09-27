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

import multiprocessing
import os
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Protocol

from .pairwise import estimate_offset
from .params import DEFAULT_PARAMS, DEFAULT_SOLVER_PARAMS, SolverParams, SyncParams
from .solver import clock_sigma, clock_starts, solve_placements
from .types import (
    ClipInput,
    ClockReading,
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
class CandidatePair:
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
        workers: int = 1,
    ) -> list[PairwiseMatch]:
        """Match every pair of clips that could overlap."""
        self._validate(clips)
        if self.options.mode == SyncMode.TIMECODE:
            return []
        excluded = corrections.excluded_clips if corrections else set()
        pairs = self.candidate_pairs([c for c in clips if c.clip_id not in excluded])
        return self.match_pairs(pairs, progress=progress, cancel=cancel, workers=workers)

    def match_pairs(
        self,
        pairs: Sequence[CandidatePair],
        *,
        progress: ProgressCallback | None = None,
        cancel: CancelToken | None = None,
        workers: int = 1,
        pool: Executor | None = None,
        on_match: Callable[[PairwiseMatch], None] | None = None,
    ) -> list[PairwiseMatch]:
        """Match the given pairs, in worker processes when ``pool`` is given or ``workers > 1``.

        Results come back in the order of ``pairs`` whatever order they finish
        in; ``on_match`` sees each as soon as it is ready (to persist it).
        Matching is CPU-bound code that holds the GIL, so threads do not help;
        processes do. Pass a long-lived ``pool`` from :func:`create_match_pool`
        to avoid paying worker start-up (about 1 s) on every run.
        """
        results: list[PairwiseMatch | None] = [None] * len(pairs)

        def done(k: int, match: PairwiseMatch, finished: int) -> None:
            results[k] = match
            if on_match is not None:
                on_match(match)
            if progress is not None:
                progress(finished / len(pairs), f"Matched {match.tgt_id} against {match.ref_id}")

        if pool is not None and pairs:
            self._match_parallel(pairs, pool, done, cancel)
        elif workers > 1 and len(pairs) >= 2 * workers:
            with create_match_pool(workers) as own_pool:
                self._match_parallel(pairs, own_pool, done, cancel)
        else:
            for k, pair in enumerate(pairs):
                if cancel is not None and cancel.is_set():
                    raise SyncCancelled()
                done(k, self.match_pair(pair.ref, pair.tgt, window=pair.window), k + 1)
        if progress is not None:
            progress(1.0, f"Matched {len(pairs)} pairs")
        return [m for m in results if m is not None]

    def _match_parallel(
        self,
        pairs: Sequence[CandidatePair],
        pool: Executor,
        done: Callable[[int, PairwiseMatch, int], None],
        cancel: CancelToken | None,
    ) -> None:
        futures = {pool.submit(_match_task, self.options, p.ref, p.tgt, p.window): k for k, p in enumerate(pairs)}
        try:
            for finished, future in enumerate(as_completed(futures), start=1):
                if cancel is not None and cancel.is_set():
                    raise SyncCancelled()
                done(futures[future], future.result(), finished)
        finally:
            for future in futures:
                future.cancel()  # no-op for finished or running tasks

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
        fallback: bool | None = None,
    ) -> PairwiseMatch:
        """Match two clips; ``window`` restricts ``start(tgt) - start(ref)``.

        Also used by the UI to "snap" a roughly dragged clip: pass a narrow
        window around the dragged position. ``fallback`` overrides
        ``SyncOptions.fallback_full_search`` (a window that came from the audio
        itself, not from a clock, is not worth a full search when it fails).
        """
        use_fallback = self.options.fallback_full_search if fallback is None else fallback
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
        if audio_window is not None and use_fallback and estimate.status != MatchStatus.CONFIDENT:
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

    def candidate_pairs(self, clips: Sequence[ClipInput]) -> list[CandidatePair]:
        """Pairs worth matching, with clock-derived search windows (hybrid mode)."""
        opts = self.options
        starts = clock_starts(clips) if opts.mode == SyncMode.HYBRID else {}
        audio_clips = [c for c in clips if c.audio is not None]
        pairs: list[CandidatePair] = []
        for i, ref in enumerate(audio_clips):
            for tgt in audio_clips[i + 1 :]:
                if opts.skip_same_device_pairs and ref.device_id is not None and ref.device_id == tgt.device_id:
                    continue
                window = None
                prior = self._clock_prior(ref, tgt, starts)
                if prior is not None:
                    predicted, margin = prior
                    # The clocks say these clips cannot overlap: skip the pair.
                    if predicted > ref.duration_s + margin or predicted + tgt.duration_s < -margin:  # type: ignore[operator]
                        continue
                    window = (predicted - margin, predicted + margin)
                pairs.append(CandidatePair(ref, tgt, window))
        return pairs

    def _clock_prior(
        self, ref: ClipInput, tgt: ClipInput, starts: dict[tuple[str, ClockReading], float]
    ) -> tuple[float, float] | None:
        """Predicted ``start(tgt) - start(ref)`` and its search half-width, from the
        most precise clock domain both clips share (None if they share none)."""
        solver = self.options.solver
        tgt_clocks = {c.domain: c for c in tgt.clocks}
        best: tuple[float, float] | None = None
        for rc in ref.clocks:
            tc = tgt_clocks.get(rc.domain)
            if tc is None or (ref.clip_id, rc) not in starts or (tgt.clip_id, tc) not in starts:
                continue
            margin = self.options.clock_search_margin_s + 3.0 * (clock_sigma(rc, solver) + clock_sigma(tc, solver))
            if best is None or margin < best[1]:
                best = (starts[(tgt.clip_id, tc)] - starts[(ref.clip_id, rc)], margin)
        return best

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


_SINGLE_THREAD_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


def create_match_pool(workers: int | None = None) -> ProcessPoolExecutor:
    """A warmed-up pool of matcher processes.

    Workers use single-threaded maths libraries (N processes × N library
    threads on N cores made parallel matching 3× *slower* than serial), and
    they are started together up front rather than one by one on demand.
    """
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    saved = {k: os.environ.get(k) for k in _SINGLE_THREAD_ENV}
    os.environ.update(_SINGLE_THREAD_ENV)
    try:
        pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
        for future in [pool.submit(_warm_up) for _ in range(workers)]:
            future.result()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return pool


def _warm_up() -> None:
    import time

    time.sleep(0.05)  # hold this worker so the next warm-up task starts another one


def _match_task(
    options: SyncOptions, ref: ClipInput, tgt: ClipInput, window: tuple[float, float] | None
) -> PairwiseMatch:
    return SyncEngine(options).match_pair(ref, tgt, window=window)


def verify_pair(
    options: SyncOptions,
    ref: ClipInput,
    tgt: ClipInput,
    windows: Sequence[tuple[float, float] | None],
    stage: str,
) -> PairwiseMatch:
    """Match a planned pair (see :mod:`.candidates`): try each window until one gives a confident match; return
    the most confident result. Only clock windows fall back to a full search (flagged ``clock_mismatch``)."""
    engine = SyncEngine(options)
    best: PairwiseMatch | None = None
    for window in windows or [None]:
        match = engine.match_pair(ref, tgt, window=window, fallback=stage == "clock")
        if best is None or match.confidence > best.confidence:
            best = match
        if match.status == MatchStatus.CONFIDENT:
            break
    return best  # type: ignore[return-value]
