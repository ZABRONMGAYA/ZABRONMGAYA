"""Which pairs of clips to compare: a funnel that scales to thousands of clips.

Comparing every pair is the most thorough plan and is used while it is cheap (``EXHAUSTIVE_PAIR_BUDGET``). Beyond
that, the number of pairs grows with the square of the clip count (4,200 clips: 8.8 million pairs, weeks of work),
while the pairs that actually overlap grow only with the clip count. The funnel finds those directly:

1. **Clocks**: clips whose shared clocks (jam-synced timecode) predict an overlap, found with a sweep over time
   rather than by testing every pair; verified inside the predicted window.
2. **Landmarks**: clips whose audio fingerprints vote for a common offset (:mod:`.landmarks`); verified by the full
   matcher inside a narrow window around that offset.
3. **Extended search**: clips still without a confident match get a full search against a bounded number of likely
   partners: the clips their fingerprints pointed to most, and the longest recordings (usually the sound
   recorders) nearest in recording time.
4. What is still unmatched is reported for manual sync, never guessed.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .engine import SyncEngine
from .landmarks import Candidate
from .solver import clock_starts
from .types import ClipInput, SyncMode

#: Compare every cross-device pair while there are at most this many (about 25 clips on a few devices).
EXHAUSTIVE_PAIR_BUDGET = 300
#: Half-width of the verification window around a landmark offset, before the drift allowance.
LANDMARK_RADIUS_S = 0.5
#: Largest clock drift tracked (100 ppm), as a window allowance per second of overlap.
DRIFT_ALLOWANCE = 100e-6
#: Extended-search partners per unmatched clip.
FALLBACK_PARTNERS = 6


@dataclass
class PlannedPair:
    """A pair to verify: ``start(tgt) - start(ref)`` is searched in each window in turn (None: everywhere)."""

    ref: str
    tgt: str
    windows: list[tuple[float, float] | None] = field(default_factory=list)
    stage: str = "full"  # full | clock | landmark | fallback
    votes: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.ref, self.tgt)


def cross_device_pairs(clips: Sequence[ClipInput]) -> int:
    """How many pairs an exhaustive search would compare (clips with audio, on different devices)."""
    per_device: dict[str | None, int] = defaultdict(int)
    n = 0
    for c in clips:
        if c.audio is not None:
            per_device[c.device_id if c.device_id is not None else f"clip:{c.clip_id}"] += 1
            n += 1
    return (n * (n - 1) - sum(k * (k - 1) for k in per_device.values())) // 2


def exhaustive_pairs(engine: SyncEngine, clips: Sequence[ClipInput]) -> list[PlannedPair]:
    """Every cross-device pair (clock windows where clocks predict one, else a full search)."""
    return [
        PlannedPair(p.ref.clip_id, p.tgt.clip_id, [p.window], "clock" if p.window is not None else "full")
        for p in engine.candidate_pairs(clips)
    ]


def clock_pairs(engine: SyncEngine, clips: Sequence[ClipInput]) -> list[PlannedPair]:
    """Cross-device pairs that shared clocks predict to overlap, found by sweeping each clock domain in time order."""
    if engine.options.mode != SyncMode.HYBRID:
        return []
    audio = [c for c in clips if c.audio is not None]
    starts = clock_starts(audio)
    by_id = {c.clip_id: c for c in audio}
    by_domain: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for (clip_id, clock), start in starts.items():
        clip = by_id[clip_id]
        by_domain[clock.domain].append((start, start + float(clip.duration_s or 0.0), clip_id))
    margin = engine.options.clock_search_margin_s
    seen: set[tuple[str, str]] = set()
    out: list[PlannedPair] = []
    for members in by_domain.values():
        if len({by_id[m[2]].device_id for m in members}) < 2:
            continue  # one device's own clock: its clips never overlap each other
        members.sort()
        active: list[tuple[float, float, str]] = []  # (end, start, id), sorted by end
        for start, end, clip_id in members:
            cut = bisect.bisect_left(active, (start - margin,))
            del active[:cut]
            for _, _, other in active:
                a, b = by_id[other], by_id[clip_id]
                if a.device_id is not None and a.device_id == b.device_id:
                    continue
                pair = (other, clip_id) if other < clip_id else (clip_id, other)
                if pair in seen:
                    continue
                prior = engine._clock_prior(by_id[pair[0]], by_id[pair[1]], starts)
                if prior is None:
                    continue
                predicted, m = prior
                ref, tgt = by_id[pair[0]], by_id[pair[1]]
                if predicted > ref.duration_s + m or predicted + tgt.duration_s < -m:  # type: ignore[operator]
                    continue
                seen.add(pair)
                out.append(PlannedPair(pair[0], pair[1], [(predicted - m, predicted + m)], "clock"))
            bisect.insort(active, (end, start, clip_id))
    return out


def landmark_pairs(
    found: dict[str, list[Candidate]],
    clips: dict[str, ClipInput],
    *,
    radius_s: float = LANDMARK_RADIUS_S,
    per_pair: int = 2,
) -> list[PlannedPair]:
    """Pairs from landmark votes, with narrow clip-level windows around the voted offsets.

    Each overlapping pair is usually found from both sides; the two votes are merged. ``Candidate.offset_s`` is
    ``start(query) - start(other)`` between the two audio streams; windows are for ``start(tgt) - start(ref)``
    between the clips (the audio may start after the clip, see ``ClipInput.audio_start_s``).
    """
    hyps: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    for query, cands in found.items():
        q = clips.get(query)
        if q is None:
            continue
        for c in cands:
            o = clips.get(c.other)
            if o is None:
                continue
            # clip offset start(q) - start(o) = audio offset - (audio_start(q) - audio_start(o))
            clip_off = c.offset_s - (q.audio_start_s - o.audio_start_s)
            if o.clip_id < q.clip_id:
                hyps[(o.clip_id, q.clip_id)].append((clip_off, c.votes))
            else:
                hyps[(q.clip_id, o.clip_id)].append((-clip_off, c.votes))
    out: list[PlannedPair] = []
    for (ref, tgt), offsets in hyps.items():
        merged: list[tuple[float, int]] = []
        for off, votes in sorted(offsets, key=lambda ov: -ov[1]):
            if all(abs(off - m) > 0.2 for m, _ in merged):
                merged.append((off, votes))
        merged = merged[:per_pair]
        overlap = min(float(clips[ref].duration_s or 0.0), float(clips[tgt].duration_s or 0.0))
        r = radius_s + DRIFT_ALLOWANCE * overlap
        out.append(PlannedPair(ref, tgt, [(off - r, off + r) for off, _ in merged], "landmark", merged[0][1]))
    return out


def merge_plans(*plans: Iterable[PlannedPair]) -> list[PlannedPair]:
    """One plan per pair; windows from every source are tried (clock windows first, then landmark windows)."""
    by_pair: dict[tuple[str, str], PlannedPair] = {}
    for plan in plans:
        for p in plan:
            key = (p.ref, p.tgt) if p.ref < p.tgt else (p.tgt, p.ref)
            if key != (p.ref, p.tgt):
                p = PlannedPair(key[0], key[1], [None if w is None else (-w[1], -w[0]) for w in p.windows],
                                p.stage, p.votes)  # fmt: skip
            existing = by_pair.get(key)
            if existing is None:
                by_pair[key] = PlannedPair(p.ref, p.tgt, list(p.windows), p.stage, p.votes)
            else:
                existing.windows += [w for w in p.windows if w not in existing.windows]
                existing.votes = max(existing.votes, p.votes)
    return list(by_pair.values())


def fallback_partners(
    unmatched: Sequence[str],
    clips: dict[str, ClipInput],
    *,
    weak_votes: dict[str, list[Candidate]] | None = None,
    creation_time: dict[str, float] | None = None,
    already: set[tuple[str, str]] | None = None,
    k: int = FALLBACK_PARTNERS,
) -> list[PlannedPair]:
    """Extended search for clips with no confident match: up to ``k`` full searches each.

    Partners, in order: clips the fingerprint pointed to (even below the candidate threshold), then the longest
    recordings on other devices nearest in recording time (or longest overall when times are unknown).
    """
    weak_votes = weak_votes or {}
    creation_time = creation_time or {}
    already = already or set()
    audio = [c for c in clips.values() if c.audio is not None]
    longest = sorted(audio, key=lambda c: -float(c.duration_s or 0.0))
    long_pool = longest[: max(4 * k, 32)]
    out: list[PlannedPair] = []
    for clip_id in unmatched:
        me = clips[clip_id]
        if me.audio is None:
            continue
        options = [c.other for c in sorted(weak_votes.get(clip_id, []), key=lambda c: -c.total_votes)]
        t = creation_time.get(clip_id)
        pool = long_pool if t is None else sorted(
            long_pool, key=lambda c, t=t: abs(creation_time.get(c.clip_id, float("inf")) - t)
        )  # fmt: skip
        chosen: list[str] = []
        for source, limit in ((options, k // 2), ([c.clip_id for c in pool], k)):
            for other in source:
                if len(chosen) >= limit:
                    break
                if _usable_partner(me, clips.get(other), chosen, already):
                    chosen.append(other)
        for other in chosen:
            ref, tgt = (clip_id, other) if clip_id < other else (other, clip_id)
            out.append(PlannedPair(ref, tgt, [None], "fallback"))
    return out


def _usable_partner(me: ClipInput, other: ClipInput | None, chosen: list[str], already: set[tuple[str, str]]) -> bool:
    if other is None or other.audio is None or other.clip_id == me.clip_id or other.clip_id in chosen:
        return False
    if me.device_id is not None and other.device_id == me.device_id:
        return False
    a, b = me.clip_id, other.clip_id
    return ((a, b) if a < b else (b, a)) not in already
