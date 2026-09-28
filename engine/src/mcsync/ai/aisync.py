"""AI sync: placing a clip that audio fingerprinting could not place, from other evidence.

* **Speech**: the same sentences in two recordings (transcripts matched by their words) give the offset between
  them; several sentences agreeing make a candidate.
* **Visual**: the same changes of light seen by two cameras (see :mod:`mcsync.ai.visual`).
* **Audio**: at a candidate offset, the audio is compared again within ±2 s: speech that fingerprints missed
  (a distant camera microphone, reverberation) often still correlates once the search is that narrow.
* **Metadata**: the cameras' clocks, as a sanity check.

Each candidate is an offset of the clip against another clip already on a timeline, with the evidence for it and a
confidence. A candidate is a proposal: it is only used once the user accepts it, or, with the automatic fallback,
shown for review (never as a certain placement).
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import numpy as np

BUCKETS = 48  # heat cells per evidence lane (Sync UI specification §3.3)
_MIN_WORDS = 3
_MIN_SIMILARITY = 0.6
_CLUSTER_S = 0.8
_WEIGHTS = {"speech": 0.9, "visual": 0.75, "audio": 1.0, "metadata": 0.25}


@dataclass
class Segment:
    start_s: float
    end_s: float
    text: str


@dataclass
class Placed:
    """A clip already on a timeline, with its transcript."""

    clip_id: int
    group: int
    start_s: float  # in its group's time
    duration_s: float
    segments: list[Segment]


@dataclass
class Evidence:
    lane: str  # speech | visual | audio | metadata
    score: float  # 0–1
    note: str
    times: list[tuple[float, float]] = field(default_factory=list)  # where in the clip (s), with a strength 0–1


@dataclass
class Candidate:
    anchor_clip_id: int
    group: int
    offset_s: float  # the clip's start relative to the anchor's start
    start_s: float  # the clip's start in the group's time
    evidence: list[Evidence]
    confidence: float = 0.0

    def lanes(self, duration_s: float) -> dict[str, list[int]]:
        """Heat cells per lane: 0 none, 1 weak, 2 partial, 3 strong."""
        out = {}
        for e in self.evidence:
            cells = [0] * BUCKETS
            for t, strength in e.times:
                k = min(BUCKETS - 1, max(0, int(t / max(duration_s, 1e-6) * BUCKETS)))
                cells[k] = max(cells[k], 3 if strength >= 0.8 else 2 if strength >= 0.6 else 1)
            out[e.lane] = cells
        return out

    def to_dict(self, duration_s: float) -> dict:
        return {
            "anchor_clip_id": self.anchor_clip_id,
            "group": self.group,
            "offset_s": self.offset_s,
            "start_s": self.start_s,
            "confidence": self.confidence,
            "evidence": [{"lane": e.lane, "score": e.score, "note": e.note} for e in self.evidence],
            "lanes": self.lanes(duration_s),
        }


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", text))


def _shingles(words: list[str], n: int = 3) -> set[tuple[str, ...]]:
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[k : k + n]) for k in range(len(words) - n + 1)}


@dataclass
class _SpeechMatch:
    other: Placed
    target_t: float
    start_s: float  # implied start of the target in the group's time
    similarity: float
    words: int
    text: str


def speech_candidates(target: list[Segment], placed: list[Placed]) -> list[Candidate]:
    """Candidates from sentences heard both in the clip and in clips already on a timeline."""
    index: dict[tuple[str, ...], list[tuple[Placed, Segment, str]]] = defaultdict(list)
    for p in placed:
        for s in p.segments:
            norm = normalise(s.text)
            for sh in _shingles(norm.split()):
                index[sh].append((p, s, norm))
    matches: list[_SpeechMatch] = []
    for seg in target:
        norm = normalise(seg.text)
        words = norm.split()
        if len(words) < _MIN_WORDS:
            continue
        seen: set[tuple[int, float]] = set()
        for sh in _shingles(words):
            for p, s, other_norm in index.get(sh, ()):
                key = (p.clip_id, s.start_s)
                if key in seen:
                    continue
                seen.add(key)
                sim = SequenceMatcher(None, norm, other_norm, autojunk=False).ratio()
                if sim >= _MIN_SIMILARITY:
                    matches.append(_SpeechMatch(p, seg.start_s, p.start_s + s.start_s - seg.start_s, sim, len(words),
                                                seg.text))  # fmt: skip
    # Sentences that agree on the clip's start (within _CLUSTER_S) make one candidate.
    by_group: dict[int, list[_SpeechMatch]] = defaultdict(list)
    for m in matches:
        by_group[m.other.group].append(m)
    out: list[Candidate] = []
    for group, ms in by_group.items():
        ms.sort(key=lambda m: m.start_s)
        clusters: list[list[_SpeechMatch]] = []
        for m in ms:
            if clusters and m.start_s - clusters[-1][-1].start_s <= _CLUSTER_S:
                clusters[-1].append(m)
            else:
                clusters.append([m])
        for c in clusters:
            per_sentence: dict[float, _SpeechMatch] = {}
            for m in c:  # one vote per sentence of the clip, its best match
                if m.target_t not in per_sentence or m.similarity > per_sentence[m.target_t].similarity:
                    per_sentence[m.target_t] = m
            votes = list(per_sentence.values())
            weights = np.array([m.similarity * min(1.0, m.words / 8) for m in votes])
            start = float(np.average([m.start_s for m in votes], weights=weights))
            anchor = max(votes, key=lambda m: m.similarity).other
            strength = float(1 - np.exp(-weights.sum()))
            quote = max(votes, key=lambda m: m.similarity * m.words).text
            note = (f"“{_shorten(quote)}” in both" if len(votes) == 1
                    else f"{len(votes)} sentences in both, such as “{_shorten(quote)}”")  # fmt: skip
            where = [(m.target_t, m.similarity) for m in votes]
            out.append(Candidate(anchor.clip_id, group, start - anchor.start_s, start,
                                 [Evidence("speech", strength, note, where)]))  # fmt: skip
    return out


def _shorten(text: str, n: int = 48) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def score(candidate: Candidate) -> float:
    """Combined confidence: evidence of different kinds reinforces; without the audio agreeing, at most 0.95."""
    miss = 1.0
    for e in candidate.evidence:
        miss *= 1 - _WEIGHTS[e.lane] * max(0.0, min(1.0, e.score))
    has_audio = any(e.lane == "audio" and e.score >= 0.7 for e in candidate.evidence)
    return round(min(0.99 if has_audio else 0.95, 1 - miss), 3)


def metadata_evidence(candidate: Candidate, clock_offset_s: float | None) -> Evidence | None:
    """How the cameras' clocks compare with the candidate offset (``clock_offset_s``: the clip's recording start
    minus the anchor's, from their metadata)."""
    if clock_offset_s is None:
        return None
    diff = abs(candidate.offset_s - clock_offset_s)
    if diff <= 120:
        return Evidence("metadata", 0.6 * (1 - diff / 120), f"camera clocks agree within {diff:.0f} s")
    minutes = diff / 60
    return Evidence("metadata", 0.0, f"camera clocks differ by {minutes:.0f} min (not set?)" if minutes < 600 else
                    "camera clocks disagree")  # fmt: skip


def rank(candidates: list[Candidate]) -> list[Candidate]:
    for c in candidates:
        c.confidence = score(c)
    return sorted(candidates, key=lambda c: -c.confidence)


def agreement(candidates: list[Candidate]) -> float | None:
    """How much stronger the best candidate is than the next (``None`` with a single candidate)."""
    if len(candidates) < 2 or candidates[1].confidence <= 0:
        return None
    return candidates[0].confidence / candidates[1].confidence


ProgressFn = Callable[[float, str], None]
