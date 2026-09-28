#!/usr/bin/env bash
# FFmpeg with encoders on a CI runner (the tests generate footage with it), plus any extra apt packages given as
# arguments on Linux. On Windows, Chocolatey's feed is tried three times; when it stays down (it has returned
# 504s), the Windows build published on GitHub by BtbN/FFmpeg-Builds is used instead.
set -euo pipefail

case "$RUNNER_OS" in
  Linux)
    sudo apt-get update -q
    sudo apt-get install -y -q ffmpeg "$@"
    ;;
  macOS)
    brew install ffmpeg
    ;;
  Windows)
    for attempt in 1 2 3; do
      if choco install ffmpeg -y --no-progress && command -v ffmpeg >/dev/null; then break; fi
      echo "Chocolatey could not install FFmpeg (attempt $attempt of 3)"
      if [ "$attempt" -lt 3 ]; then sleep $((attempt * 20)); fi
    done
    if ! command -v ffmpeg >/dev/null; then
      echo "Installing FFmpeg from BtbN/FFmpeg-Builds instead"
      tmp=$(cygpath -u "$RUNNER_TEMP")
      curl -fsSL --retry 5 --retry-delay 10 -o "$tmp/ffmpeg.zip" \
        https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip
      pwsh -NoProfile -Command "Expand-Archive -Path '$(cygpath -w "$tmp/ffmpeg.zip")' -DestinationPath '$(cygpath -w "$tmp/ffmpeg")' -Force"
      bin=$(dirname "$(find "$tmp/ffmpeg" -name ffmpeg.exe | head -1)")
      cygpath -w "$bin" >> "$GITHUB_PATH" # for the next steps
      export PATH="$bin:$PATH"
    fi
    ;;
esac
ffmpeg -hide_banner -version | head -1
