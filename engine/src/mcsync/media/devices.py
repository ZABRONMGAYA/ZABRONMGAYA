"""Which device recorded each file, which files belong to one take, and
whether a device's timecode can be trusted as a clock.

Device identity matters to synchronisation in three ways: clips from one device
never overlap (their pair is not matched), they share the device's clock (an
interrupted clip without usable audio is placed through its siblings), and a
device's clips go on one timeline track.

Identity, most to least reliable:

1. serial number (Sony/Canon XML sidecars, some QuickTime tags);
2. make + model + memory card;
3. file-name family (GoPro, DJI, Sony, Canon, Zoom, …) + card or folder;
4. folder + kind (audio-only files in a folder are one recorder).

The user can always reassign devices; this only has to be right by default.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .probe import MediaInfo

_CARD_MARKERS = {"DCIM", "PRIVATE", "CONTENTS", "AVCHD", "CLIP", "XDROOT", "M4ROOT"}
_CARD_DEPTH = 5
_PHONE_MAKES = {"apple", "samsung", "google", "huawei", "xiaomi", "oneplus", "oppo", "motorola", "sony mobile"}
_DRONE_MODELS = re.compile(r"mavic|air|mini|phantom|inspire|avata|matrice|fpv|fc\d{4}", re.IGNORECASE)

# (family, pattern on the file stem)
_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GoPro", re.compile(r"^(G[HXL]\d{2}\d{4}|GOPR\d{4}|GP\d{2}\d{4})$", re.IGNORECASE)),
    ("DJI", re.compile(r"^DJI_", re.IGNORECASE)),
    ("Sony", re.compile(r"^(C\d{4}|\d{8}_C\d{4}|MAH\d{5})$", re.IGNORECASE)),
    ("Canon", re.compile(r"^(MVI_\d{4}|[A-Z]\d{3}C\d{3}_\w+)$", re.IGNORECASE)),
    ("Panasonic", re.compile(r"^P\d{7}$", re.IGNORECASE)),
    ("Blackmagic", re.compile(r"^[A-Z]\d{3}_\d{8}_C\d{3}$", re.IGNORECASE)),
    ("iPhone", re.compile(r"^IMG_\d{4}$", re.IGNORECASE)),
    ("Android", re.compile(r"^(VID_\d{8}_\d{6}|PXL_\d{8}_\d+)", re.IGNORECASE)),
    ("Zoom", re.compile(r"^(ZOOM\d{4}|\d{6}_\d{3,4})(_Tr\w+)?$", re.IGNORECASE)),
    ("Tascam", re.compile(r"^TASCAM_\d{4}", re.IGNORECASE)),
)

_GOPRO_NEW = re.compile(r"^(G[HXL])(\d{2})(\d{4})$", re.IGNORECASE)
_GOPRO_OLD_FIRST = re.compile(r"^GOPR(\d{4})$", re.IGNORECASE)
_GOPRO_OLD_NEXT = re.compile(r"^GP(\d{2})(\d{4})$", re.IGNORECASE)
_FAT32_LIMIT = 4 * 2**30
_SPLIT_MIN_SIZE = int(0.93 * _FAT32_LIMIT)


@dataclass(frozen=True)
class DeviceGuess:
    key: str  # stable identity, used as the engine's device_id
    name: str  # display name
    kind: str  # camera | recorder | phone | drone
    make: str | None = None
    model: str | None = None
    serial: str | None = None


def file_family(path: str | Path) -> str | None:
    stem = Path(path).stem
    return next((family for family, pattern in _FAMILIES if pattern.match(stem)), None)


def card_root(path: str | Path) -> Path:
    """The memory card (or copied card folder) a file came from; else its folder.

    Card layouts nest markers (``PRIVATE/M4ROOT/CLIP``, ``PRIVATE/AVCHD/BDMV``),
    so the outermost marker wins, searched only a few levels up so that a user
    folder that happens to be called ``CLIP`` cannot swallow a whole drive.
    """
    p = Path(path).resolve()
    root = p.parent
    for parent in list(p.parents)[:_CARD_DEPTH]:
        if parent.name.upper() in _CARD_MARKERS:
            root = parent.parent
    return root


def _kind(info: MediaInfo, family: str | None) -> str:
    if info.is_audio_only:
        return "recorder"
    make = (info.make or "").lower()
    if family in ("iPhone", "Android") or make in _PHONE_MAKES:
        return "phone"
    if (make == "dji" or family == "DJI") and (not info.audio or _DRONE_MODELS.search(info.model or "")):
        return "drone"
    return "camera"


def identify_device(info: MediaInfo) -> DeviceGuess:
    family = file_family(info.path)
    kind = _kind(info, family)
    card = card_root(info.path)
    label = " ".join(x for x in (info.make, info.model) if x) or family
    if info.serial:
        key = f"serial:{info.make or ''}:{info.model or ''}:{info.serial}"
        return DeviceGuess(key, f"{label or 'Device'} #{info.serial[-4:]}", kind, info.make, info.model, info.serial)
    if info.make or info.model:
        key = f"model:{info.make or ''}:{info.model or ''}:{card}"
        return DeviceGuess(key, f"{label} ({card.name})", kind, info.make, info.model)
    key = f"folder:{card}:{kind}:{family or ''}"
    name = f"{family or kind.capitalize()} ({card.name})"
    return DeviceGuess(key, name, kind)


# ---------------------------------------------------------------------------
# Chapters: one continuous recording split into several files
# ---------------------------------------------------------------------------


def find_chapters(infos: Sequence[MediaInfo], device_keys: Sequence[str]) -> list[list[int]]:
    """Groups of indices into ``infos`` that are consecutive chapters of one take.

    Cameras split long takes (GoPro at ~12 minutes, FAT32 cards at 4 GB) into
    files that follow each other without a gap. Returns groups of two or more
    files, each in recording order.
    """
    groups: dict[tuple, list[tuple[int, int]]] = defaultdict(list)  # key -> [(chapter number, index)]
    claimed: set[int] = set()
    for i, info in enumerate(infos):
        path = Path(info.path)
        stem = path.stem
        if m := _GOPRO_NEW.match(stem):
            groups[(str(path.parent), "gopro", m.group(1).upper(), m.group(3))].append((int(m.group(2)), i))
        elif m := _GOPRO_OLD_FIRST.match(stem):
            groups[(str(path.parent), "gopro-old", m.group(1))].append((0, i))
        elif m := _GOPRO_OLD_NEXT.match(stem):
            groups[(str(path.parent), "gopro-old", m.group(2))].append((int(m.group(1)), i))
    out: list[list[int]] = []
    for members in groups.values():
        if len(members) > 1:
            ordered = [i for _, i in sorted(members)]
            out.append(ordered)
            claimed.update(ordered)

    # Generic 4 GB splits: same device, previous file at the FAT32 limit, and the
    # next one created when the previous one ends.
    by_device: dict[str, list[int]] = defaultdict(list)
    for i, key in enumerate(device_keys):
        if i not in claimed and infos[i].creation_time is not None:
            by_device[key].append(i)
    for members in by_device.values():
        members.sort(key=lambda i: (infos[i].creation_time, infos[i].path))  # type: ignore[arg-type, return-value]
        current: list[int] = [members[0]] if members else []
        for prev, nxt in zip(members, members[1:], strict=False):
            a, b = infos[prev], infos[nxt]
            gap = (b.creation_time - a.creation_time).total_seconds() - a.duration_s  # type: ignore[operator]
            if a.size_bytes >= _SPLIT_MIN_SIZE and abs(gap) <= 3.0:
                current.append(nxt)
            else:
                if len(current) > 1:
                    out.append(current)
                current = [nxt]
        if len(current) > 1:
            out.append(current)
    return out


# ---------------------------------------------------------------------------
# Timecode trust
# ---------------------------------------------------------------------------


def is_record_run(infos: Sequence[MediaInfo], chapters: Sequence[Sequence[int]] = ()) -> bool:
    """True if one device's timecode only advanced while recording ("rec run").

    Rec-run timecode makes consecutive takes look contiguous (take 2 starts on
    the frame after take 1 ends) whatever the pause between them, so it cannot
    place clips relative to each other. Detected when every pair of consecutive
    takes (chapters of one take excluded) is contiguous to within two frames.
    """
    same_take = {frozenset((a, b)) for group in chapters for a, b in zip(group, group[1:], strict=False)}
    timed = sorted(
        (i for i, info in enumerate(infos) if info.timecode is not None), key=lambda i: infos[i].timecode.seconds
    )  # type: ignore[union-attr]
    contiguous, pairs = 0, 0
    for a, b in zip(timed, timed[1:], strict=False):
        if frozenset((a, b)) in same_take:
            continue
        ta, tb = infos[a].timecode, infos[b].timecode
        assert ta is not None and tb is not None
        frame = float(1 / ta.rate) if ta.rate else 0.04
        pairs += 1
        if abs(tb.seconds - (ta.seconds + infos[a].duration_s)) <= 2 * frame:
            contiguous += 1
    return pairs > 0 and contiguous == pairs
