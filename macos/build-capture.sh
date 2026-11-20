#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd -P)"
output="${1:-$root/data/apps}"
mkdir -p "$output"
stage="$(mktemp -d "$output/.capture-build.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
xcrun clang -O2 -fobjc-arc -dynamiclib -mmacosx-version-min=14.0 \
    -Wl,-install_name,@rpath/libwowagent_capture.dylib \
    -framework Foundation -framework CoreGraphics -framework CoreMedia -framework CoreVideo \
    -framework ScreenCaptureKit "$root/macos/WindowCapture.m" -o "$stage/libwowagent_capture.dylib"
mv "$stage/libwowagent_capture.dylib" "$output/libwowagent_capture.dylib"
