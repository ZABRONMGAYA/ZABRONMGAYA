"""From files on disk to the engine's :class:`~mcsync.sync.ClipInput` list.

``scan_media`` → probe, fingerprint, devices, chapters.
``extract_audio`` → cached analysis signals.
``build_clip_inputs`` → clips with every clock reading they carry:

* **timecode / BWF**: per device (``tc:<device>:<family>``) unless the user
  says the devices were jam-synced (``tc:<family>``); dropped for devices whose
  timecode is rec-run;
* **creation time**: always per device (``ct:<device>``), since camera clocks are set by hand;
* **chapter position**: ``chapter:<take>``, exact to the frame.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from mcsync.sync.engine import CancelToken
from mcsync.sync.params import DEFAULT_PARAMS, SyncParams
from mcsync.sync.signal import AnalysisSignal
from mcsync.sync.types import ClipInput, ClockReading, ClockSource

from .cache import AnalysisCache
from .devices import DeviceGuess, find_chapters, identify_device, is_record_run
from .extract import extract_to_cache
from .fingerprint import fingerprint
from .probe import AudioStreamInfo, MediaInfo, ProbeError, is_media_candidate, probe
from .tools import FFmpegTools, find_tools

#: Uncertainty of a Broadcast WAV time reference between files of one recorder (sample-counted: 0.1 ms).
BWF_DEVICE_SIGMA_S = 1e-4


@dataclass
class MediaItem:
    info: MediaInfo
    fingerprint: str
    device: DeviceGuess
    audio_stream: AudioStreamInfo | None
    channel: int | None = None  # None = downmix
    #: ``(take id, position)`` when the file is one chapter of a longer take.
    chapter: tuple[str, int] | None = None
    #: Start of this chapter within its take (sum of the preceding chapters).
    chapter_offset_s: float = 0.0
    signal: AnalysisSignal | None = field(default=None, repr=False)

    @property
    def path(self) -> str:
        return self.info.path


@dataclass(frozen=True)
class ScanProblem:
    path: str
    message: str


def _expand(paths: Iterable[str | Path], recursive: bool) -> list[Path]:
    found: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            walker = p.rglob("*") if recursive else p.iterdir()
            found.extend(sorted(f for f in walker if is_media_candidate(f)))
        elif is_media_candidate(p):
            found.append(p)
    unique: dict[Path, Path] = {}
    for f in found:
        unique.setdefault(f.resolve(), f)
    return list(unique.values())


def scan_media(
    paths: Iterable[str | Path],
    *,
    tools: FFmpegTools | None = None,
    recursive: bool = True,
    workers: int | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> tuple[list[MediaItem], list[ScanProblem]]:
    """Probe every media file under ``paths``. Files ffprobe rejects are reported, not fatal."""
    tools = tools or find_tools()
    files = _expand(paths, recursive)
    problems: list[ScanProblem] = []
    infos: list[MediaInfo] = []
    prints: list[str] = []

    def work(f: Path) -> tuple[MediaInfo, str] | ScanProblem | None:
        try:
            return probe(f, tools), fingerprint(f)
        except ProbeError as exc:
            return None if exc.skip else ScanProblem(str(f), exc.reason)  # not footage: left out quietly
        except OSError as exc:
            return ScanProblem(str(f), str(exc))

    with ThreadPoolExecutor(max_workers=workers or min(8, os.cpu_count() or 2)) as pool:
        for k, result in enumerate(pool.map(work, files)):
            if progress is not None:
                progress((k + 1) / max(len(files), 1), f"Reading {files[k].name}")
            if result is None:
                continue
            if isinstance(result, ScanProblem):
                problems.append(result)
            else:
                infos.append(result[0])
                prints.append(result[1])

    devices = [identify_device(info) for info in infos]
    items = [
        MediaItem(info=info, fingerprint=fp, device=dev, audio_stream=info.primary_audio)
        for info, fp, dev in zip(infos, prints, devices, strict=True)
    ]
    for group in find_chapters(infos, [d.key for d in devices]):
        offset = 0.0
        take_id = f"{devices[group[0]].key}#{Path(infos[group[0]].path).name}"
        for position, i in enumerate(group):
            items[i].chapter = (take_id, position)
            items[i].chapter_offset_s = offset
            offset += infos[i].duration_s
            # A take comes from one device, whatever the per-file heuristics said.
            items[i].device = items[group[0]].device
    return items, problems


def extract_audio(
    items: Sequence[MediaItem],
    cache: AnalysisCache,
    *,
    params: SyncParams = DEFAULT_PARAMS,
    tools: FFmpegTools | None = None,
    workers: int | None = None,
    cancel: CancelToken | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> None:
    """Fill ``item.signal`` for every item with audio (cached after the first time)."""
    tools = tools or find_tools()
    todo = [it for it in items if it.audio_stream is not None]

    def work(it: MediaItem) -> AnalysisSignal:
        entry = cache.entry(it.fingerprint, it.audio_stream.index, channel=it.channel, params=params)  # type: ignore[union-attr]
        return extract_to_cache(
            it.info, entry, stream=it.audio_stream, channel=it.channel, params=params, tools=tools, cancel=cancel
        )

    with ThreadPoolExecutor(max_workers=workers or max(1, (os.cpu_count() or 2) // 2)) as pool:
        for k, (it, sig) in enumerate(zip(todo, pool.map(work, todo), strict=True)):
            it.signal = sig
            if progress is not None:
                progress((k + 1) / len(todo), f"Extracted audio from {Path(it.path).name}")


def build_clip_inputs(
    items: Sequence[MediaItem],
    *,
    timecode_jam_synced: bool = False,
    use_creation_time: bool = True,
    clip_ids: Sequence[str] | None = None,
) -> list[ClipInput]:
    """Engine inputs for ``items``; clip ids default to the file paths."""
    if clip_ids is not None and len(clip_ids) != len(items):
        raise ValueError("clip_ids must match items")
    by_device: dict[str, list[int]] = defaultdict(list)
    for i, it in enumerate(items):
        by_device[it.device.key].append(i)
    rec_run: set[str] = set()
    for key, members in by_device.items():
        infos = [items[i].info for i in members]
        local = {i: k for k, i in enumerate(members)}
        takes: dict[str, list[int]] = defaultdict(list)
        for i in members:
            if items[i].chapter is not None:
                takes[items[i].chapter[0]].append(local[i])  # type: ignore[index]
        if is_record_run(infos, list(takes.values())):
            rec_run.add(key)

    clips: list[ClipInput] = []
    for k, it in enumerate(items):
        clocks: list[ClockReading] = []
        tc = it.info.timecode
        if tc is not None and it.device.key not in rec_run:
            domain = f"tc:{tc.family}" if timecode_jam_synced else f"tc:{it.device.key}:{tc.family}"
            source = ClockSource.BWF if tc.source == "bwf" else ClockSource.TIMECODE
            sigma = float(1 / tc.rate) if tc.rate else None
            if source == ClockSource.BWF and not timecode_jam_synced:
                # A recorder's own sample count: its split files follow each other to the sample.
                sigma = BWF_DEVICE_SIGMA_S
            clocks.append(ClockReading(tc.seconds, domain=domain, source=source, sigma_s=sigma))
        if use_creation_time and it.info.creation_time is not None:
            clocks.append(
                ClockReading(
                    it.info.creation_time.timestamp(), domain=f"ct:{it.device.key}", source=ClockSource.CREATION_TIME
                )
            )
        if it.chapter is not None:
            clocks.append(
                ClockReading(it.chapter_offset_s, domain=f"chapter:{it.chapter[0]}", source=ClockSource.CHAPTER)
            )
        clips.append(
            ClipInput(
                clip_id=clip_ids[k] if clip_ids is not None else it.path,
                audio=it.signal,
                duration_s=it.info.duration_s,
                device_id=it.device.key,
                clock=clocks[0] if clocks else None,
                extra_clocks=tuple(clocks[1:]),
                audio_start_s=it.info.audio_start_s(it.audio_stream) if it.audio_stream else 0.0,
            )
        )
    return clips
