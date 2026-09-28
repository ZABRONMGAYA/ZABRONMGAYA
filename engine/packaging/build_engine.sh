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
# The frozen engine transcribes with the shipped models (the installer puts them next to it, as here), with the
# FFmpeg built for the app when there is one (packaging/build_ffmpeg.sh), else the one on PATH.
here="$(pwd -W 2>/dev/null || pwd)" # a Windows path under Git Bash: the frozen engine is a Windows program
[ -d dist/ffmpeg ] && export MCSYNC_FFMPEG_DIR="$here/dist/ffmpeg"
MCSYNC_MODELS_DIR="$here/dist/models" "$exe" --cache-dir build/check-cache transcribe tests/data/dialog.ogg \
  | tee build/transcribe-check.txt
grep -qi "happy couple" build/transcribe-check.txt
