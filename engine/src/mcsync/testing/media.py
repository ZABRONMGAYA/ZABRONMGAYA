"""Real media files with known ground truth, generated with FFmpeg.

Builds on :mod:`mcsync.testing.synthetic`: each device's audio is recorded from
one synthetic scene, then muxed with a tiny test-pattern video into the
container a real device would produce (MOV with timecode, MP4, AVCHD MPEG-TS,
MXF, GoPro chapters, Broadcast WAV with a time reference).
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import zlib
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

from mcsync.timecode import Timecode, parse_frame_rate

from .synthetic import Scene, make_scene, record

#: Scene time 0 is this time of day on the shoot date.
SHOOT_DATE = dt.datetime(2026, 6, 14, 10, 0, 0, tzinfo=dt.UTC)
SHOOT_TIME_OF_DAY = 10 * 3600.0


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run_ffmpeg(args: list[str], pcm: np.ndarray | None = None) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-v", "error", *args],
        input=None if pcm is None else np.ascontiguousarray(pcm, dtype="<f4").tobytes(),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace"))


def timecode_label(scene_time: float, rate: Fraction, drop_frame: bool = False) -> str:
    """Timecode a jam-synced device shows at ``scene_time`` (frames/rate semantics)."""
    return str(Timecode.from_seconds(SHOOT_TIME_OF_DAY + scene_time, rate, drop_frame))


def creation_time(scene_time: float, clock_error_s: float = 0.0) -> str:
    """ISO creation date written by a camera whose clock is ``clock_error_s`` off (1 s resolution)."""
    when = SHOOT_DATE + dt.timedelta(seconds=int(scene_time + clock_error_s))
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_bwf(
    path: Path, scene: Scene, start_s: float, duration_s: float, *, rate: int = 48000, channels: int = 1, **record_kw
) -> None:
    """Broadcast WAV with a bext time reference (recorders)."""
    pcm = record(scene, start_s=start_s, duration_s=duration_s, rate=rate, channels=channels, **record_kw)
    run_ffmpeg(
        [
            "-f", "f32le", "-ar", str(rate), "-ac", str(channels), "-i", "pipe:0",
            "-c:a", "pcm_s24le", "-write_bext", "1",
            "-metadata", f"time_reference={int(round((SHOOT_TIME_OF_DAY + start_s) * rate))}",
            "-metadata", "originator=ZOOM", "-metadata", "originator_reference=F6",
            str(path),
        ],
        pcm,
    )  # fmt: skip


def write_camera_clip(
    path: Path,
    scene: Scene | None,
    start_s: float,
    duration_s: float,
    *,
    frame_rate: str = "25",
    audio_codec: tuple[str, ...] = ("aac", "-b:a", "96k"),
    container: str | None = None,
    timecode: str | None = None,
    created: str | None = None,
    audio_delay_s: float = 0.0,
    video_codec: tuple[str, ...] = ("libx264", "-preset", "ultrafast"),
    extra: tuple[str, ...] = (),
    **record_kw,
) -> None:
    """A camera file: test-pattern video starting at scene time ``start_s`` plus
    the scene's audio (none when ``scene`` is None), optionally delayed."""
    args = ["-f", "lavfi", "-i", f"testsrc=size=64x36:rate={frame_rate}:duration={duration_s}"]
    pcm = None
    if scene is not None:
        record_kw.setdefault("seed", zlib.crc32(path.name.encode()) % 10_000)  # stable across runs
        pcm = record(
            scene, start_s=start_s + audio_delay_s, duration_s=duration_s - audio_delay_s, rate=48000, **record_kw
        )
        args += (["-itsoffset", str(audio_delay_s)] if audio_delay_s else []) + [
            "-f", "f32le", "-ar", "48000", "-ac", "1", "-i", "pipe:0",
        ]  # fmt: skip
    args += ["-map", "0:v"] + (["-map", "1:a"] if pcm is not None else []) + ["-c:v", *video_codec]
    if pcm is not None:
        args += ["-c:a", *audio_codec]
    if timecode:
        args += ["-timecode", timecode]
    if created:
        args += ["-metadata", f"creation_time={created}"]
    args += list(extra) + (["-f", container] if container else []) + [str(path)]
    run_ffmpeg(args, pcm)


@dataclass
class Shoot:
    root: Path
    scene: Scene
    #: Scene time at which each file (relative path) starts.
    truth: dict[str, float] = field(default_factory=dict)
    reference: str = ""

    def path(self, rel: str) -> str:
        return str(self.root / rel)

    def expected(self, rel: str) -> float:
        """Expected placement relative to the reference clip."""
        return self.truth[rel] - self.truth[self.reference]


def generate_wedding_shoot(root: Path, *, seed: int = 21) -> Shoot:
    """A small but realistic shoot covering the containers and metadata the app must handle.

    * ``ZOOM/230614_001.WAV``: 48 kHz BWF recorder with a time reference (reference clip);
    * ``CAM_A``: MOV, 23.976 fps, jam-synced timecode, creation times 97 s fast, interrupted twice;
    * ``CAM_B/C0001.MP4``: 29.97 drop-frame timecode, audio starting 0.25 s after the video;
    * ``CAM_C/PRIVATE/AVCHD/BDMV/STREAM/00001.MTS``: AVCHD 50p with AC-3 audio (streams start at 1.4 s);
    * ``GOPRO/DCIM/100GOPRO/GH01…/GH02…``: two chapters of one take, the second one muted;
    * ``DRONE/DJI_0001.MP4``: no audio, jam-synced 30 fps timecode.
    """
    scene = make_scene(420.0, kind="mixed", rate=16000, seed=seed)
    shoot = Shoot(root=root, scene=scene, reference="ZOOM/230614_001.WAV")
    film, ntsc, thirty = parse_frame_rate("23.976"), parse_frame_rate("29.97"), parse_frame_rate("30")

    def add(rel: str, start: float) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shoot.truth[rel] = start
        return path

    write_bwf(add("ZOOM/230614_001.WAV", 20.0), scene, 20.0, 380.0, snr_db=30, seed=1)
    for name, start, duration in (("A001.MOV", 30.0, 100.0), ("A002.MOV", 180.5, 120.0)):
        write_camera_clip(
            add(f"CAM_A/{name}", start), scene, start, duration,
            frame_rate="24000/1001", timecode=timecode_label(start, film), created=creation_time(start, 97.0),
            snr_db=12, highpass_hz=200, reverb_rt60_s=0.6,
        )  # fmt: skip
    write_camera_clip(
        add("CAM_B/C0001.MP4", 60.2), scene, 60.2, 150.0,
        frame_rate="30000/1001", timecode=timecode_label(60.2, ntsc, True), audio_delay_s=0.25, snr_db=8,
    )  # fmt: skip
    write_camera_clip(
        add("CAM_C/PRIVATE/AVCHD/BDMV/STREAM/00001.MTS", 250.0), scene, 250.0, 90.0,
        frame_rate="50", audio_codec=("ac3",), container="mpegts", snr_db=10,
    )  # fmt: skip
    write_camera_clip(add("GOPRO/DCIM/100GOPRO/GH010042.MP4", 40.0), scene, 40.0, 70.0, frame_rate="30", snr_db=10)
    write_camera_clip(add("GOPRO/DCIM/100GOPRO/GH020042.MP4", 110.0), scene, 110.0, 60.0, frame_rate="30", gain_db=-120)
    write_camera_clip(
        add("DRONE/DJI_0001.MP4", 300.0), None, 300.0, 40.0, frame_rate="30", timecode=timecode_label(300.0, thirty)
    )
    return shoot
