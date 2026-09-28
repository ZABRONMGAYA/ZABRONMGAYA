"""Transcription: where people speak (Silero VAD), what they say (Whisper), and a voice fingerprint per utterance
(3D-Speaker ERes2Net) that groups utterances by speaker.

A recording is transcribed in chunks of ``CHUNK_S`` seconds, one pipeline task each, so long recordings show
progress and survive a pause or a quit. Each chunk decodes a little audio on both sides: an utterance belongs to
the chunk it starts in, and is transcribed whole. Segment times come from the voice activity detector (accurate to
a few hundredths of a second), not from Whisper's coarse timestamps.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from mcsync.media.tools import FFmpegTools, subprocess_flags

from .models import ModelError, ModelStore

SAMPLE_RATE = 16_000
CHUNK_S = 600.0
#: Audio decoded before a chunk (context for the voice detector) and after it (so an utterance that starts in the
#: chunk is transcribed whole; utterances are at most MAX_UTTERANCE_S long).
CHUNK_HEAD_S = 2.0
MAX_UTTERANCE_S = 20.0
CHUNK_TAIL_S = MAX_UTTERANCE_S + 2.0
#: Utterances shorter than this give unreliable voice fingerprints: they get none (and no speaker).
MIN_FINGERPRINT_S = 1.0
_BATCH = 8
_VAD_WINDOW = 512  # Silero's window at 16 kHz

#: Whisper's language codes and names (the languages it was trained on).
LANGUAGES = {
    "en": "English", "sw": "Swahili", "fr": "French", "es": "Spanish", "ar": "Arabic", "pt": "Portuguese",
    "de": "German", "it": "Italian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "hi": "Hindi",
    "am": "Amharic", "yo": "Yoruba", "nl": "Dutch", "ru": "Russian", "tr": "Turkish", "pl": "Polish",
    "uk": "Ukrainian", "vi": "Vietnamese", "id": "Indonesian", "th": "Thai", "he": "Hebrew", "el": "Greek",
    "cs": "Czech", "ro": "Romanian", "hu": "Hungarian", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "ms": "Malay", "tl": "Tagalog", "ta": "Tamil", "te": "Telugu", "ur": "Urdu",
    "bn": "Bengali", "fa": "Persian", "so": "Somali", "ha": "Hausa", "zu": "Zulu",
    "af": "Afrikaans", "ca": "Catalan", "hr": "Croatian", "sr": "Serbian", "sk": "Slovak", "sl": "Slovenian",
    "bg": "Bulgarian", "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian", "mr": "Marathi", "gu": "Gujarati",
    "pa": "Punjabi", "ne": "Nepali", "si": "Sinhala", "my": "Myanmar", "km": "Khmer", "lo": "Lao",
    "ka": "Georgian", "hy": "Armenian", "az": "Azerbaijani", "kk": "Kazakh", "uz": "Uzbek", "mn": "Mongolian",
    "ln": "Lingala", "sn": "Shona", "mg": "Malagasy", "haw": "Hawaiian", "mi": "Maori", "cy": "Welsh",
    "ga": "Irish", "is": "Icelandic", "mt": "Maltese", "sq": "Albanian", "mk": "Macedonian", "bs": "Bosnian",
    "gl": "Galician", "eu": "Basque", "yue": "Cantonese",
}  # fmt: skip

# Whisper writes non-speech sounds as bracketed captions: kept as markers, not as transcript text.
_CAPTION = re.compile(r"^\s*[\[(♪*]+\s*(?P<what>[^\])♪*]*?)\s*[\])♪*]+\s*$")
_EVENTS = (("applause", "Applause"), ("clap", "Applause"), ("cheer", "Cheering"), ("laugh", "Laughter"),
           ("music", "Music"), ("sing", "Singing"))  # fmt: skip
_NOISE = {"", ".", "...", "you", "thank you.", "thanks for watching!", "thank you for watching."}


@dataclass
class Utterance:
    start_s: float  # in the clip's time
    end_s: float
    text: str
    language: str | None
    fingerprint: np.ndarray | None  # float32, unit length


@dataclass
class SoundEvent:
    t_s: float
    label: str  # Applause, Music, ...


class SpeechUnavailable(ModelError):
    pass


class Transcriber:
    """The models for one speech model and language, loaded once and shared by the transcription tasks."""

    def __init__(self, store: ModelStore, model_id: str, language: str = "auto", threads: int = 2) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - the packaged app always has it
            raise SpeechUnavailable("The speech engine (sherpa-onnx) is not installed") from exc
        speech, vad, voice = store.path(model_id), store.path("silero-vad"), store.path("voice-eres2net")
        missing = [m for m, p in ((model_id, speech), ("silero-vad", vad), ("voice-eres2net", voice)) if p is None]
        if missing:
            raise SpeechUnavailable(f"Model not installed: {', '.join(missing)}")
        assert speech is not None and vad is not None and voice is not None
        self.model_id = model_id
        self.language = "" if language in ("", "auto", None) else language
        self._sherpa = sherpa_onnx
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(speech / "encoder.onnx"),
            decoder=str(speech / "decoder.onnx"),
            tokens=str(speech / "tokens.txt"),
            language=self.language,
            task="transcribe",
            num_threads=threads,
        )
        self._vad_model = str(vad / "silero_vad.onnx")
        self._voice = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(voice / "voice.onnx"), num_threads=threads)
        )
        self._lock = threading.Lock()  # one chunk at a time per model

    def _vad(self):  # noqa: ANN202
        cfg = self._sherpa.VadModelConfig()
        cfg.silero_vad.model = self._vad_model
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.3
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = MAX_UTTERANCE_S
        cfg.sample_rate = SAMPLE_RATE
        return self._sherpa.VoiceActivityDetector(cfg, buffer_size_in_seconds=MAX_UTTERANCE_S * 3)

    def transcribe_range(
        self,
        tools: FFmpegTools,
        path: str,
        stream_index: int,
        channel: int | None,
        channels: int,
        start_s: float,
        end_s: float,
        cancel: threading.Event | None = None,
        progress: Callable[[float], None] | None = None,
    ) -> tuple[list[Utterance], list[SoundEvent]]:
        """The utterances that start in ``[start_s, end_s)`` of a clip, with their text and voice fingerprints."""
        decode_from = max(0.0, start_s - CHUNK_HEAD_S)
        pieces: list[tuple[float, np.ndarray]] = []
        vad = self._vad()

        def take() -> None:
            while not vad.empty():
                t = decode_from + vad.front.start / SAMPLE_RATE
                if start_s <= t < end_s:
                    pieces.append((t, np.array(vad.front.samples, dtype=np.float32)))
                vad.pop()

        cmd = decode_command(tools, path, stream_index, channel, channels, decode_from,
                             end_s + CHUNK_TAIL_S - decode_from)  # fmt: skip
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **subprocess_flags())  # fmt: skip
        assert proc.stdout is not None
        try:
            span = max(1.0, end_s + CHUNK_TAIL_S - decode_from)
            decoded = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise TranscriptionCancelled()
                buf = proc.stdout.read(SAMPLE_RATE * 4 * 4)  # 4 s
                if not buf:
                    break
                samples = np.frombuffer(buf[: len(buf) // 4 * 4], dtype="<f4")
                # One detector window at a time: fed larger blocks, it merges utterances and misplaces their start.
                for k in range(0, len(samples), _VAD_WINDOW):
                    vad.accept_waveform(samples[k : k + _VAD_WINDOW])
                    take()
                decoded += len(samples)
                if progress is not None:
                    progress(0.3 * min(1.0, decoded / SAMPLE_RATE / span))
            vad.flush()
            take()
        finally:
            proc.kill()
            proc.wait()
        utterances: list[Utterance] = []
        events: list[SoundEvent] = []
        with self._lock:
            for k in range(0, len(pieces), _BATCH):
                if cancel is not None and cancel.is_set():
                    raise TranscriptionCancelled()
                batch = pieces[k : k + _BATCH]
                streams = []
                for _, x in batch:
                    s = self._recognizer.create_stream()
                    s.accept_waveform(SAMPLE_RATE, x)
                    streams.append(s)
                self._recognizer.decode_streams(streams)
                for (t, x), s in zip(batch, streams, strict=True):
                    text = " ".join(s.result.text.split())
                    lang = getattr(s.result, "lang", "") or self.language or None
                    event = caption_event(text)
                    if event is not None:
                        events.append(SoundEvent(t, event))
                        continue
                    if text.lower() in _NOISE or _CAPTION.match(text) or repetitive(text):
                        continue
                    utterances.append(Utterance(t, t + len(x) / SAMPLE_RATE, text, lang, self._fingerprint(x)))
                if progress is not None:
                    progress(0.3 + 0.7 * min(1.0, (k + len(batch)) / max(1, len(pieces))))
        return utterances, events

    def _fingerprint(self, x: np.ndarray) -> np.ndarray | None:
        if len(x) < MIN_FINGERPRINT_S * SAMPLE_RATE:
            return None
        s = self._voice.create_stream()
        s.accept_waveform(SAMPLE_RATE, x)
        s.input_finished()
        v = np.asarray(self._voice.compute(s), dtype=np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None


class TranscriptionCancelled(Exception):
    pass


def caption_event(text: str) -> str | None:
    """ "(applause)", "[Music]", "♪" → a sound-event label; ``None`` for speech."""
    if text.strip() and set(text.strip()) <= set("♪ "):
        return "Music"
    m = _CAPTION.match(text)
    if not m:
        return None
    what = m.group("what").lower()
    return next((label for key, label in _EVENTS if key in what), None)


def repetitive(text: str) -> bool:
    """Whisper's hallucination on noise: one or two words over and over ("of of of of of")."""
    words = re.findall(r"\w+", text.lower())
    return len(words) >= 4 and len(set(words)) <= max(1, len(words) // 4)


def decode_command(
    tools: FFmpegTools, path: str, stream_index: int, channel: int | None, channels: int, start_s: float, dur_s: float
) -> list[str]:
    """Decode part of one audio stream to 16 kHz mono float32 (what the speech models read)."""
    if channel is not None:
        mix = [f"pan=mono|c0=c{channel}"]
    elif channels > 1:
        mix = ["pan=mono|c0=" + "+".join(f"{1 / channels:.10g}*c{k}" for k in range(channels))]
    else:
        mix = []
    cmd = [tools.ffmpeg, "-nostdin", "-hide_banner", "-v", "error"]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-t", f"{dur_s:.3f}", "-i", path, "-map", f"0:{stream_index}", "-vn", "-sn", "-dn"]
    if mix:
        cmd += ["-af", ",".join(mix)]
    return cmd + ["-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]


def chunks(duration_s: float) -> list[tuple[float, float]]:
    """The ``[start, end)`` ranges a recording is transcribed in."""
    n = max(1, int(np.ceil(max(duration_s, 0.001) / CHUNK_S)))
    return [(k * CHUNK_S, min(duration_s, (k + 1) * CHUNK_S) if k < n - 1 else max(duration_s, k * CHUNK_S + 1))
            for k in range(n)]  # fmt: skip


def language_name(code: str | None) -> str | None:
    return LANGUAGES.get(code or "", code) if code else None
