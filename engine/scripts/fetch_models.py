"""Download the AI models that ship with Syncora (or others) into a folder.

    python scripts/fetch_models.py                 # the bundled models, into engine/models (used in development)
    python scripts/fetch_models.py DEST            # e.g. engine/dist/models, packaged by electron-builder
    python scripts/fetch_models.py DEST --models whisper-tiny silero-vad

A model already in DEST is kept. Sources and sizes: mcsync.ai.models.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mcsync.ai.models import ALL_MODELS, ModelStore


def _printer():  # noqa: ANN202
    """Download progress, printed every 10 %."""
    last = [-1]

    def progress(received: int, total: int) -> None:
        pct = int(100 * received / total) if total else 0
        if pct // 10 != last[0]:
            last[0] = pct // 10
            print(f"  {pct}%", flush=True)

    return progress


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dest", nargs="?", default=str(Path(__file__).resolve().parents[1] / "models"))
    parser.add_argument("--models", nargs="*", default=[m.id for m in ALL_MODELS.values() if m.bundled])
    args = parser.parse_args()
    dest = Path(args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    os.environ["MCSYNC_MODELS_DIR"] = str(dest)  # what is already there counts as installed
    store = ModelStore(dest)
    for model_id in args.models:
        spec = ALL_MODELS[model_id]
        if store.path(model_id) is not None:
            print(f"{model_id}: already in {dest}")
            continue
        print(f"{model_id}: downloading {spec.download_bytes / 1e6:.0f} MB from {spec.url}", flush=True)
        path = store.download(model_id, progress=_printer())
        size = sum(f.stat().st_size for f in path.iterdir())
        print(f"{model_id}: installed in {path} ({size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
