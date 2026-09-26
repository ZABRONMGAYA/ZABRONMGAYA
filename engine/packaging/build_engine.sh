#!/usr/bin/env bash
# Freezes the engine into dist/mcsync-engine/ (an executable plus its libraries) with PyInstaller.
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
