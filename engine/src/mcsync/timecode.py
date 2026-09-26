"""SMPTE timecode and frame-rate arithmetic.

Frame rates are exact :class:`fractions.Fraction` values (29.97 is
``30000/1001``), so frame/second conversions never accumulate rounding error.
The sync engine works in float seconds; conversions to frames happen only at
the edges (metadata import, timeline export).

Drop-frame timecode (29.97 and 59.94 only) skips frame *labels*, not frames:
the first 2 (or 4) labels of every minute except each tenth minute. That keeps
the label within a few frames of wall-clock time: ``01:00:00;00`` is 107 892
frames = 3599.9964 s. Non-drop 29.97 timecode ``01:00:00:00`` is 108 000
frames = 3603.6 s of real time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

#: Common production frame rates by conventional name.
STANDARD_RATES: dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001),
    "24": Fraction(24),
    "25": Fraction(25),
    "29.97": Fraction(30000, 1001),
    "30": Fraction(30),
    "47.952": Fraction(48000, 1001),
    "48": Fraction(48),
    "50": Fraction(50),
    "59.94": Fraction(60000, 1001),
    "60": Fraction(60),
    "100": Fraction(100),
    "119.88": Fraction(120000, 1001),
    "120": Fraction(120),
}
_SNAP_TOLERANCE = 5e-4  # relative; NTSC rates differ from integers by 1e-3

_TC_RE = re.compile(r"^(-)?(\d{1,2}):(\d{2}):(\d{2})([:;.,])(\d{2,3})$")


def parse_frame_rate(value: str | float | int | Fraction | tuple[int, int]) -> Fraction:
    """Parse a frame rate as reported by ffprobe, NLEs or users.

    Accepts ``"30000/1001"``, ``"29.97"``, ``29.97``, ``(30000, 1001)`` and
    ``Fraction``. Decimal inputs within 0.05 % of a standard rate snap to it,
    so ``29.97`` and ``29.970029`` both become ``30000/1001``.
    """
    if isinstance(value, Fraction):
        rate = value
    elif isinstance(value, tuple):
        rate = Fraction(value[0], value[1])
    elif isinstance(value, int):
        rate = Fraction(value)
    elif isinstance(value, str) and "/" in value:
        num, den = value.split("/", 1)
        if int(den) == 0:
            raise ValueError(f"invalid frame rate {value!r}")
        rate = Fraction(int(num), int(den))
    else:
        as_float = float(value)
        rate = Fraction(as_float).limit_denominator(1001)
        for std in STANDARD_RATES.values():
            if abs(as_float - float(std)) <= _SNAP_TOLERANCE * float(std):
                rate = std
                break
    if rate <= 0:
        raise ValueError(f"frame rate must be positive, got {value!r}")
    return rate


def nominal_fps(rate: Fraction) -> int:
    """Frames per timecode second (30 for 29.97, 24 for 23.976)."""
    return int(round(rate))


def supports_drop_frame(rate: Fraction) -> bool:
    """Drop-frame labelling is defined for 29.97 and its multiples (59.94, 119.88)."""
    return rate.denominator == 1001 and nominal_fps(rate) % 30 == 0


def _drop_per_minute(rate: Fraction) -> int:
    return 2 * nominal_fps(rate) // 30


@dataclass(frozen=True, order=True)
class Timecode:
    hours: int
    minutes: int
    seconds: int
    frames: int
    drop_frame: bool = False

    @classmethod
    def parse(cls, text: str, drop_frame: bool | None = None) -> Timecode:
        """Parse ``HH:MM:SS:FF`` (non-drop) or ``HH:MM:SS;FF`` (drop-frame).

        ``.`` and ``,`` before the frames also mean drop-frame, as some tools
        write them. ``drop_frame`` overrides the separator when given.
        """
        m = _TC_RE.match(text.strip())
        if not m or m.group(1):
            raise ValueError(f"invalid timecode {text!r}")
        hh, mm, ss, sep, ff = int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5), int(m.group(6))
        if mm > 59 or ss > 59:
            raise ValueError(f"invalid timecode {text!r}")
        df = sep != ":" if drop_frame is None else drop_frame
        return cls(hh, mm, ss, ff, df)

    def __str__(self) -> str:
        sep = ";" if self.drop_frame else ":"
        return f"{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}{sep}{self.frames:02d}"

    def to_frames(self, rate: Fraction) -> int:
        """Frame count since 00:00:00:00 at ``rate``."""
        fps = nominal_fps(rate)
        if self.frames >= fps:
            raise ValueError(f"{self} has frame {self.frames} but the rate has {fps} frames per second")
        total = (self.hours * 3600 + self.minutes * 60 + self.seconds) * fps + self.frames
        if not self.drop_frame:
            return total
        if not supports_drop_frame(rate):
            raise ValueError(f"drop-frame timecode is not defined at {float(rate):.3f} fps")
        drop = _drop_per_minute(rate)
        if self.seconds == 0 and self.minutes % 10 != 0 and self.frames < drop:
            raise ValueError(f"{self} does not exist in drop-frame timecode")
        total_minutes = self.hours * 60 + self.minutes
        return total - drop * (total_minutes - total_minutes // 10)

    @classmethod
    def from_frames(cls, frames: int, rate: Fraction, drop_frame: bool = False) -> Timecode:
        """Timecode label of frame number ``frames`` (wraps at 24 hours)."""
        if frames < 0:
            raise ValueError("frame count must be non-negative")
        fps = nominal_fps(rate)
        if drop_frame:
            if not supports_drop_frame(rate):
                raise ValueError(f"drop-frame timecode is not defined at {float(rate):.3f} fps")
            drop = _drop_per_minute(rate)
            per_10min = fps * 600 - drop * 9
            per_min = fps * 60 - drop
            frames %= per_10min * 144  # 24 h
            tens, rem = divmod(frames, per_10min)
            extra = drop * 9 * tens
            if rem > drop:
                extra += drop * ((rem - drop) // per_min)
            frames += extra
        else:
            frames %= fps * 86400
        ff = frames % fps
        total_s = frames // fps
        return cls(total_s // 3600, (total_s // 60) % 60, total_s % 60, ff, drop_frame)

    def to_seconds(self, rate: Fraction) -> Fraction:
        """Exact real time since midnight of the frame this label names."""
        return Fraction(self.to_frames(rate)) / rate

    @classmethod
    def from_seconds(cls, seconds: Fraction | float, rate: Fraction, drop_frame: bool = False) -> Timecode:
        """Label of the frame that contains ``seconds`` (floor)."""
        frames = int(Fraction(seconds) * rate)
        return cls.from_frames(frames, rate, drop_frame)


def timecode_to_seconds(text: str, rate: str | float | Fraction, drop_frame: bool | None = None) -> float:
    """Convenience: ``"10:00:00:00"`` at 25 fps → 36000.0."""
    return float(Timecode.parse(text, drop_frame).to_seconds(parse_frame_rate(rate)))


def bwf_time_reference_to_seconds(time_reference: int, sample_rate: int) -> float:
    """Broadcast WAV ``bext`` time reference (samples since midnight) in seconds."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return time_reference / sample_rate
