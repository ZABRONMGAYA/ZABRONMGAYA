#!/usr/bin/env bash
# Builds the FFmpeg that ships inside Multicam Sync: one pinned release on every platform, LGPL, decode only.
#
#   packaging/build_ffmpeg.sh OUTPUT_DIR
#
# The same version everywhere matters: FFmpeg releases disagree about some edge-list timings, which would move clips.
# Decode only: no encoders except the PCM the engine reads its analysis audio as, so originals can never be
# re-encoded. LGPL 2.1+: no GPL or non-free parts, statically linked, with the licence and this recipe alongside.
# Needs git, a C compiler, make and nasm (macOS: Xcode tools + brew nasm; Windows: MSYS2 MinGW64).
set -euo pipefail

VERSION="${FFMPEG_VERSION:-n8.1.3}"
COMMIT="${FFMPEG_COMMIT:-1041abdc962f4cc4f394aa8de9dc5236c0c3b9e7}"
OUT="$(mkdir -p "${1:?usage: build_ffmpeg.sh OUTPUT_DIR}" && cd "$1" && pwd)"
WORK="${FFMPEG_WORK:-$(mktemp -d)}"

if [ ! -d "$WORK/src/.git" ]; then
  git clone --quiet --depth 1 --branch "$VERSION" https://github.com/FFmpeg/FFmpeg.git "$WORK/src"
fi
cd "$WORK/src"
actual="$(git rev-parse HEAD)"
if [ "$actual" != "$COMMIT" ]; then
  echo "FFmpeg $VERSION is commit $actual, expected $COMMIT" >&2
  exit 1
fi

FLAGS=(
  --disable-gpl --disable-nonfree          # LGPL 2.1+ only
  --disable-autodetect --enable-zlib       # no system libraries picked up by accident (zlib: compressed MOV headers)
  --enable-static --disable-shared
  --disable-programs --enable-ffmpeg --enable-ffprobe
  --disable-doc --disable-network --disable-devices --disable-hwaccels
  --disable-encoders --enable-encoder=pcm_f32le,pcm_s16le
  --disable-muxers --enable-muxer=pcm_f32le,wav,null   # "-f f32le" is the pcm_f32le muxer
  --disable-debug
)
EXE=""
case "$(uname -s)" in
  Darwin)
    FLAGS+=(--extra-cflags=-mmacosx-version-min=12.0 --extra-ldflags=-mmacosx-version-min=12.0)
    JOBS="$(sysctl -n hw.ncpu)"
    ;;
  MINGW* | MSYS*)
    FLAGS+=(--extra-ldflags=-static)       # no MinGW runtime DLLs next to the executables
    EXE=".exe"
    JOBS="$(nproc)"
    ;;
  *)
    JOBS="$(nproc)"
    ;;
esac

./configure "${FLAGS[@]}" >"$WORK/configure.log" || { tail -30 "$WORK/configure.log" ffbuild/config.log >&2; exit 1; }
make -j"$JOBS" >"$WORK/make.log" 2>&1 || { tail -40 "$WORK/make.log" >&2; exit 1; }

# Everything the engine relies on (media/extract.py, media/probe.py) must be in the build.
require() {  # require KIND NAME...: each NAME must appear in `ffmpeg -KIND`
  local kind="$1" listing
  shift
  listing="$("./ffmpeg$EXE" -hide_banner "-$kind" 2>/dev/null)"
  for name in "$@"; do
    grep -Eq "^ *[A-Z.|-]+ +$name( |,|$)" <<<"$listing" || { echo "FFmpeg build lacks $kind $name" >&2; exit 1; }
  done
}
require muxers f32le
require encoders pcm_f32le
require filters aresample pan
require demuxers mov,mp4,m4a,3gp,3g2,mj2 mpegts wav w64 aiff mp3 flac matroska,webm avi mxf ogg
require decoders aac ac3 eac3 mp3 flac alac opus vorbis pcm_s16le pcm_s24le pcm_s32le pcm_f32le h264 hevc prores

cp "ffmpeg$EXE" "ffprobe$EXE" "$OUT/"
cp COPYING.LGPLv2.1 "$OUT/LICENSE-FFmpeg.txt"
{
  echo "FFmpeg $VERSION (commit $COMMIT), built for Multicam Sync."
  echo "Source: https://github.com/FFmpeg/FFmpeg/tree/$VERSION"
  echo "Licence: GNU LGPL 2.1 or later (LICENSE-FFmpeg.txt). To rebuild or replace it, run"
  echo "engine/packaging/build_ffmpeg.sh from the Multicam Sync sources, which configures FFmpeg with:"
  echo "  ${FLAGS[*]}"
} >"$OUT/README-FFmpeg.txt"
"$OUT/ffmpeg$EXE" -hide_banner -version | sed -n 1p # not head: with pipefail, SIGPIPE would fail the build
