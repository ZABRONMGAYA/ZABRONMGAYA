"""Speakers: utterances grouped by voice across the whole project.

Each utterance with a voice fingerprint joins the speaker whose average voice is closest, when close enough, or
starts a new speaker. Speakers whose voices turn out the same are then merged. Online grouping keeps the cost
linear in the number of utterances (tens of thousands in a multi-day production).

The thresholds are cosine similarities of 3D-Speaker ERes2Net fingerprints, calibrated on LibriSpeech readers:
one reader's recordings scored 0.44 even across 16 and 8 kHz, two different readers 0.21 to 0.26.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAME_VOICE = 0.35
MERGE_VOICES = 0.45


@dataclass
class Speaker:
    key: str  # "S01"
    centroid: np.ndarray  # running mean of unit fingerprints (not normalised)
    count: int

    def direction(self) -> np.ndarray:
        n = float(np.linalg.norm(self.centroid))
        return self.centroid / n if n > 0 else self.centroid


def next_key(keys: set[str]) -> str:
    k = 1
    while f"S{k:02d}" in keys:
        k += 1
    return f"S{k:02d}"


def assign(fingerprints: list[np.ndarray | None], speakers: list[Speaker]) -> list[str | None]:
    """The speaker of each fingerprint (``None`` for utterances without one); ``speakers`` is updated in place."""
    keys = {s.key for s in speakers}
    out: list[str | None] = []
    for v in fingerprints:
        if v is None:
            out.append(None)
            continue
        best, score = None, -1.0
        for s in speakers:
            sim = float(s.direction() @ v)
            if sim > score:
                best, score = s, sim
        if best is not None and score >= SAME_VOICE:
            best.centroid = best.centroid + (v - best.centroid) / (best.count + 1)
            best.count += 1
            out.append(best.key)
        else:
            key = next_key(keys)
            keys.add(key)
            speakers.append(Speaker(key, v.astype(np.float32).copy(), 1))
            out.append(key)
    return out


def merges(speakers: list[Speaker]) -> list[tuple[str, str]]:
    """Pairs ``(absorbed, into)`` of speakers whose voices are the same; ``speakers`` is updated in place."""
    done: list[tuple[str, str]] = []
    changed = True
    while changed and len(speakers) > 1:
        changed = False
        dirs = np.array([s.direction() for s in speakers])
        sims = dirs @ dirs.T
        np.fill_diagonal(sims, -1.0)
        i, j = np.unravel_index(int(np.argmax(sims)), sims.shape)
        if sims[i, j] >= MERGE_VOICES:
            big, small = speakers[i], speakers[j]
            if small.count > big.count:
                big, small = small, big
            big.centroid = (big.centroid * big.count + small.centroid * small.count) / (big.count + small.count)
            big.count += small.count
            speakers.remove(small)
            done.append((small.key, big.key))
            changed = True
    return done
