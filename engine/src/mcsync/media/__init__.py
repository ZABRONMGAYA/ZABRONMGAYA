"""Reading media: probing metadata, extracting analysis audio, caching, devices.

Everything here only *reads* original media. FFmpeg and ffprobe run as child
processes (see :mod:`mcsync.media.tools`); derived data goes to the analysis
cache (:mod:`mcsync.media.cache`), never next to the originals.
"""

from .cache import AnalysisCache, CacheEntry
from .devices import DeviceGuess, find_chapters, identify_device, is_record_run
from .extract import ExtractionCancelled, ExtractionError, extract_to_cache
from .fingerprint import fingerprint
from .library import MediaItem, ScanProblem, build_clip_inputs, extract_audio, scan_media
from .probe import AudioStreamInfo, MediaInfo, ProbeError, TimecodeInfo, VideoStreamInfo, parse_probe, probe
from .tools import FFmpegNotFound, FFmpegTools, find_tools
from .waveform import PEAK_LEVELS, read_peaks

__all__ = [
    "PEAK_LEVELS",
    "AnalysisCache",
    "AudioStreamInfo",
    "CacheEntry",
    "DeviceGuess",
    "ExtractionCancelled",
    "ExtractionError",
    "FFmpegNotFound",
    "FFmpegTools",
    "MediaInfo",
    "MediaItem",
    "ScanProblem",
    "ProbeError",
    "TimecodeInfo",
    "VideoStreamInfo",
    "build_clip_inputs",
    "extract_audio",
    "extract_to_cache",
    "find_chapters",
    "find_tools",
    "fingerprint",
    "identify_device",
    "is_record_run",
    "parse_probe",
    "probe",
    "read_peaks",
    "scan_media",
]
