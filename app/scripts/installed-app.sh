#!/usr/bin/env bash
# Installs or uninstalls the macOS build the way a user would, and checks the result (release workflow).
#
#   scripts/installed-app.sh install ARCH    # copy Syncora.app from release/Syncora-<version>-macOS-ARCH.dmg
#   scripts/installed-app.sh uninstall       # move Syncora.app out of /Applications
#
# "install" writes MCSYNC_E2E_APP (the installed executable) to $GITHUB_ENV when it is set.
set -euo pipefail
cd "$(dirname "$0")/.."
version=$(node -p "require('./package.json').version")
app=/Applications/Syncora.app

case "${1:-}" in
  install)
    dmg="release/Syncora-$version-macOS-${2:?arch}.dmg"
    [ -f "$dmg" ] || { echo "no disk image at $dmg" >&2; exit 1; }
    mnt=$(mktemp -d)
    hdiutil attach -nobrowse -readonly -mountpoint "$mnt" "$dmg"
    [ -L "$mnt/Applications" ] || { echo "the disk image has no Applications shortcut" >&2; exit 1; }
    ls "$mnt/.background/"*.png >/dev/null 2>&1 || ls "$mnt/.background/"*.tiff >/dev/null 2>&1 ||
      { echo "the disk image has no background picture" >&2; exit 1; }
    ditto "$mnt/Syncora.app" "$app"
    hdiutil detach "$mnt"
    # The app's icon: named in Info.plist and present as an .icns file (not Electron's default icon).
    icon=$(/usr/libexec/PlistBuddy -c "Print CFBundleIconFile" "$app/Contents/Info.plist")
    icns="$app/Contents/Resources/${icon%.icns}.icns"
    [ "$(head -c 4 "$icns" 2>/dev/null)" = icns ] || { echo "no app icon at $icns" >&2; exit 1; }
    cmp -s "$icns" build/icon.icns || { echo "$icns is not the Syncora icon" >&2; exit 1; }
    installed=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" "$app/Contents/Info.plist")
    [ "$installed" = "$version" ] || { echo "installed version $installed, expected $version" >&2; exit 1; }
    for f in "$app/Contents/MacOS/Syncora" "$app/Contents/Resources/engine" "$app/Contents/Resources/ffmpeg"; do
      [ -e "$f" ] || { echo "missing after install: $f" >&2; exit 1; }
    done
    codesign -dv "$app" 2>&1 | grep -E "^(Identifier|Signature|TeamIdentifier)=" || true
    echo "Installed Syncora $installed in $app"
    [ -z "${GITHUB_ENV:-}" ] || echo "MCSYNC_E2E_APP=$app/Contents/MacOS/Syncora" >> "$GITHUB_ENV"
    ;;
  uninstall)
    [ -d "$app" ] || { echo "Syncora is not installed" >&2; exit 1; }
    rm -rf "$app"
    [ ! -e "$app" ] || { echo "$app is still there" >&2; exit 1; }
    echo "Uninstalled"
    ;;
  *)
    echo "usage: $0 install ARCH | uninstall" >&2
    exit 2
    ;;
esac
