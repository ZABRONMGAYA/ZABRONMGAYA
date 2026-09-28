#!/usr/bin/env bash
# Freezes the engine into dist/mcsync-engine/ (an executable plus its libraries) with PyInstaller, and puts the
# AI models Syncora ships with (speech, voice activity, voice fingerprints) in dist/models/.
#
#   packaging/build_engine.sh
#
# Run it with the Python the build should use (3.11 or newer), on the platform and architecture it is for.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"
"$PYTHON" -m pip install --quiet . "pyinstaller==6.22.3"
"$PYTHON" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath dist --workpath build/pyinstaller packaging/mcsync-engine.spec
exe="dist/mcsync-engine/mcsync-engine"
[ -f "$exe.exe" ] && exe="$exe.exe"
"$exe" --version
"$PYTHON" scripts/fetch_models.py dist/models
# The frozen engine transcribes with the shipped models (the installer puts them next to it, as here).
MCSYNC_MODELS_DIR="$PWD/dist/models" "$exe" --cache-dir build/check-cache transcribe tests/data/dialog.ogg \
  | tee build/transcribe-check.txt
grep -qi "happy couple" build/transcribe-check.txt
