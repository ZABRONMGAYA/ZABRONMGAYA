"""The models the AI features use: which are installed, where, and how to download the others.

Bundled models live in ``<app resources>/models`` (``MCSYNC_MODELS_DIR`` in development and tests); downloaded ones
in ``<cache>/models``. Each model is a folder with fixed file names, so any source (archive, single file) installs
the same way. Downloads come from the sherpa-onnx releases on GitHub, which also host the conversion recipes.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import ssl
import sys
import tarfile
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    kind: str  # "speech" | "vad" | "voice"
    title: str
    detail: str
    download_bytes: int
    url: str
    #: file name in the model folder -> pattern of the archive member (or "" for a single-file download)
    files: dict[str, str] = field(default_factory=dict)
    bundled: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "detail": self.detail,
                "download_bytes": self.download_bytes, "bundled": self.bundled}  # fmt: skip


def _whisper(size: str, title: str, detail: str, download: int, bundled: bool = False) -> ModelSpec:
    return ModelSpec(
        id=f"whisper-{size}",
        kind="speech",
        title=title,
        detail=detail,
        download_bytes=download,
        url=f"{_RELEASES}/asr-models/sherpa-onnx-whisper-{size}.tar.bz2",
        files={"encoder.onnx": "*encoder.int8.onnx", "decoder.onnx": "*decoder.int8.onnx", "tokens.txt": "*tokens.txt"},
        bundled=bundled,
    )


SPEECH_MODELS: tuple[ModelSpec, ...] = (
    _whisper("tiny", "Fast", "Whisper tiny: quickest, least accurate", 116_204_861),
    _whisper("base", "Balanced", "Whisper base: included with Syncora", 207_557_382, bundled=True),
    _whisper("small", "Accurate", "Whisper small: better with accents and other languages", 639_387_718),
    _whisper("turbo", "Best", "Whisper large-v3 turbo: most accurate, slowest", 563_790_207),
)
VAD_MODEL = ModelSpec(
    "silero-vad", "vad", "Voice activity", "Silero VAD: finds where people speak", 643_854,
    f"{_RELEASES}/asr-models/silero_vad.onnx", {"silero_vad.onnx": ""}, bundled=True,
)  # fmt: skip
VOICE_MODEL = ModelSpec(
    "voice-eres2net", "voice", "Voice fingerprints", "3D-Speaker ERes2Net: tells speakers apart", 39_593_761,
    f"{_RELEASES}/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
    {"voice.onnx": ""}, bundled=True,
)  # fmt: skip
DEFAULT_SPEECH_MODEL = "whisper-base"
ALL_MODELS = {m.id: m for m in (*SPEECH_MODELS, VAD_MODEL, VOICE_MODEL)}


class ModelError(RuntimeError):
    pass


class DownloadCancelled(ModelError):
    pass


def bundled_dir() -> Path | None:
    """Models installed with the app."""
    if env := os.environ.get("MCSYNC_MODELS_DIR"):
        return Path(env)
    if getattr(sys, "frozen", False):  # <resources>/engine/mcsync-engine[.exe] -> <resources>/models
        return Path(sys.executable).resolve().parent.parent / "models"
    dev = Path(__file__).resolve().parents[3] / "models"  # engine/models (scripts/fetch_models.py)
    return dev if dev.exists() else None


class ModelStore:
    """Where each model is, and downloads of the ones that are not there yet."""

    def __init__(self, user_dir: Path) -> None:
        self.user_dir = user_dir
        self._downloads: dict[str, dict] = {}
        self._lock = threading.Lock()

    def path(self, model_id: str) -> Path | None:
        spec = ALL_MODELS[model_id]
        for root in (bundled_dir(), self.user_dir):
            if root is not None and all((root / spec.id / name).is_file() for name in spec.files):
                return root / spec.id
        return None

    def status(self) -> list[dict]:
        with self._lock:
            downloads = {k: dict(v) for k, v in self._downloads.items()}
        out = []
        for spec in ALL_MODELS.values():
            path = self.path(spec.id)
            out.append({**spec.to_dict(), "installed": path is not None,
                        "location": "bundled" if path is not None and path.parent == bundled_dir() else
                        "downloaded" if path is not None else None,
                        "download": downloads.get(spec.id)})  # fmt: skip
        return out

    def download(self, model_id: str, cancel: threading.Event | None = None,
                 progress: Callable[[int, int], None] | None = None) -> Path:  # fmt: skip
        """Download and install ``model_id`` into the user folder (atomically: a folder appears only complete)."""
        spec = ALL_MODELS[model_id]
        if (found := self.path(model_id)) is not None:
            return found
        final = self.user_dir / spec.id
        tmp = self.user_dir / f".{spec.id}.{os.getpid()}.{int(time.time() * 1000)}.partial"
        tmp.mkdir(parents=True, exist_ok=True)
        state = {"received": 0, "total": spec.download_bytes, "error": None}
        with self._lock:
            self._downloads[model_id] = state
        try:
            with _open_url(spec.url) as response:
                total = int(response.headers.get("Content-Length") or spec.download_bytes)
                state["total"] = total
                reader = _CountingReader(response, state, cancel, progress)
                if spec.url.endswith((".tar.bz2", ".tar.gz", ".tgz")):
                    _extract(reader, spec, tmp)
                else:
                    ((name, _),) = spec.files.items()
                    with open(tmp / name, "wb") as out:
                        shutil.copyfileobj(reader, out, 1 << 20)
            missing = [n for n in spec.files if not (tmp / n).is_file()]
            if missing:
                raise ModelError(f"the download of {spec.id} lacks {', '.join(missing)}")
            shutil.rmtree(final, ignore_errors=True)
            tmp.rename(final)
            return final
        except BaseException as exc:
            state["error"] = str(exc) or type(exc).__name__
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            with self._lock:
                self._downloads.pop(model_id, None)

    def remove(self, model_id: str) -> bool:
        """Delete a downloaded model (bundled ones stay)."""
        path = self.user_dir / model_id
        if path.is_dir():
            shutil.rmtree(path)
            return True
        return False


class _CountingReader:
    def __init__(self, raw, state: dict, cancel: threading.Event | None, progress) -> None:  # noqa: ANN001
        self.raw, self.state, self.cancel, self.progress = raw, state, cancel, progress
        self._last = 0.0

    def read(self, n: int = -1) -> bytes:
        if self.cancel is not None and self.cancel.is_set():
            raise DownloadCancelled("download cancelled")
        data = self.raw.read(n if n and n > 0 else 1 << 20)
        self.state["received"] += len(data)
        now = time.monotonic()
        if self.progress is not None and (now - self._last > 0.25 or not data):
            self._last = now
            self.progress(self.state["received"], self.state["total"])
        return data


def _extract(reader: _CountingReader, spec: ModelSpec, dest: Path) -> None:
    with tarfile.open(fileobj=reader, mode="r|*") as archive:  # streamed: the archive is never stored whole
        for member in archive:
            if not member.isfile():
                continue
            base = member.name.rsplit("/", 1)[-1]
            for name, pattern in spec.files.items():
                if fnmatch.fnmatch(base, pattern) and not (dest / name).exists():
                    source = archive.extractfile(member)
                    if source is not None:
                        with open(dest / name, "wb") as out:
                            shutil.copyfileobj(source, out, 1 << 20)
                    break


def _ssl_context() -> ssl.SSLContext:
    """The system's certificates (and SSL_CERT_FILE, for networks with their own authority), plus certifi's: the
    frozen app on macOS has no system certificate paths."""
    context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    return context


def _open_url(url: str):  # noqa: ANN202
    request = urllib.request.Request(url, headers={"User-Agent": "Syncora"})
    try:
        return urllib.request.urlopen(request, timeout=60, context=_ssl_context())
    except OSError as exc:
        raise ModelError(f"Could not download {url.rsplit('/', 1)[-1]}: {exc}") from exc
