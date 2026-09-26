from fractions import Fraction

import pytest

from mcsync.timecode import (
    Timecode,
    bwf_time_reference_to_seconds,
    nominal_fps,
    parse_frame_rate,
    supports_drop_frame,
    timecode_to_seconds,
)

NTSC30 = Fraction(30000, 1001)
NTSC60 = Fraction(60000, 1001)
FILM = Fraction(24000, 1001)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30000/1001", NTSC30),
        ("29.97", NTSC30),
        (29.97, NTSC30),
        (29.970029, NTSC30),
        ("23.976", FILM),
        ("23.98", FILM),
        ("59.94", NTSC60),
        ("25", Fraction(25)),
        (50, Fraction(50)),
        ((24000, 1001), FILM),
        (Fraction(48), Fraction(48)),
        ("12.5", Fraction(25, 2)),
    ],
)
def test_parse_frame_rate(value, expected):
    assert parse_frame_rate(value) == expected


@pytest.mark.parametrize("bad", ["0", "-25", "30/0"])
def test_parse_frame_rate_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        parse_frame_rate(bad)


def test_nominal_fps_and_drop_frame_support():
    assert nominal_fps(NTSC30) == 30
    assert nominal_fps(FILM) == 24
    assert supports_drop_frame(NTSC30) and supports_drop_frame(NTSC60)
    assert not supports_drop_frame(FILM)
    assert not supports_drop_frame(Fraction(30))


def test_parse_and_format():
    tc = Timecode.parse("01:02:03:04")
    assert (tc.hours, tc.minutes, tc.seconds, tc.frames, tc.drop_frame) == (1, 2, 3, 4, False)
    assert str(tc) == "01:02:03:04"
    assert Timecode.parse("01:02:03;04").drop_frame
    assert Timecode.parse("01:02:03.04").drop_frame
    assert not Timecode.parse("01:02:03;04", drop_frame=False).drop_frame
    assert str(Timecode.parse("10:00:00;02")) == "10:00:00;02"


@pytest.mark.parametrize("bad", ["", "1:2:3", "01:60:00:00", "01:00:60:00", "aa:bb:cc:dd", "-01:00:00:00"])
def test_parse_rejects_invalid_text(bad):
    with pytest.raises(ValueError):
        Timecode.parse(bad)


@pytest.mark.parametrize(
    ("text", "rate", "frames"),
    [
        ("10:00:00:00", Fraction(25), 900_000),
        ("00:00:01:00", FILM, 24),
        ("01:00:00:00", NTSC30, 108_000),  # non-drop counts every label
        ("01:00:00;00", NTSC30, 107_892),  # drop-frame: 108 labels skipped per hour
        ("00:01:00;02", NTSC30, 1_800),  # first label after the skipped ;00 and ;01
        ("00:10:00;00", NTSC30, 17_982),  # tenth minutes skip nothing
        ("00:01:00;04", NTSC60, 3_600),  # 59.94 skips four labels
        ("23:59:59;29", NTSC30, 2_589_407),
    ],
)
def test_timecode_to_frames(text, rate, frames):
    assert Timecode.parse(text).to_frames(rate) == frames


@pytest.mark.parametrize(("rate", "drop"), [(NTSC30, True), (NTSC60, True), (NTSC30, False), (Fraction(25), False)])
def test_frames_round_trip_across_a_day(rate, drop):
    fps = nominal_fps(rate)
    last_of_day = Timecode(23, 59, 59, fps - 1, drop).to_frames(rate)
    probes = list(range(0, 3 * fps * 60 * 11)) if fps == 30 else list(range(0, fps * 60 * 11, 7))
    probes += [2_000_000, last_of_day]
    for frames in probes:
        tc = Timecode.from_frames(frames, rate, drop)
        assert tc.to_frames(rate) == frames, (frames, str(tc))
    assert Timecode.from_frames(last_of_day + 1, rate, drop) == Timecode(0, 0, 0, 0, drop)  # wraps at 24 h


def test_drop_frame_labels_that_do_not_exist():
    for text in ("00:01:00;00", "00:01:00;01", "00:59:00;01"):
        with pytest.raises(ValueError):
            Timecode.parse(text).to_frames(NTSC30)
    Timecode.parse("00:10:00;00").to_frames(NTSC30)  # tenth minutes keep their first labels


def test_drop_frame_needs_ntsc_rate_and_frames_must_fit():
    with pytest.raises(ValueError):
        Timecode.parse("00:00:01;00").to_frames(Fraction(25))
    with pytest.raises(ValueError):
        Timecode.from_frames(10, Fraction(24), drop_frame=True)
    with pytest.raises(ValueError):
        Timecode.parse("00:00:01:25").to_frames(Fraction(25))


def test_drop_frame_tracks_wall_clock():
    one_hour_df = Timecode.parse("01:00:00;00").to_seconds(NTSC30)
    one_hour_ndf = Timecode.parse("01:00:00:00").to_seconds(NTSC30)
    assert float(one_hour_df) == pytest.approx(3599.9964, abs=1e-4)
    assert float(one_hour_ndf) == pytest.approx(3603.6, abs=1e-9)


def test_seconds_round_trip_is_exact():
    tc = Timecode.parse("14:23:51:17")
    seconds = tc.to_seconds(FILM)
    assert isinstance(seconds, Fraction)
    assert Timecode.from_seconds(seconds, FILM) == tc
    assert Timecode.from_seconds(float(seconds) + 1e-9, FILM) == tc


def test_convenience_conversions():
    assert timecode_to_seconds("10:00:00:00", "25") == 36000.0
    assert timecode_to_seconds("00:00:01;00", "29.97") == pytest.approx(30 * 1001 / 30000)
    assert bwf_time_reference_to_seconds(48000 * 3600 * 10, 48000) == 36000.0
    with pytest.raises(ValueError):
        bwf_time_reference_to_seconds(1, 0)
