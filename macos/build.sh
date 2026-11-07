#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd -P)"
output="$root/data/apps"
cache="$root/data/swift-module-cache"
mkdir -p "$output" "$cache"
stage="$(mktemp -d "$root/data/.app-build.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
identity="${WOWAGENT_CODESIGN_IDENTITY:--}"
compiler=(xcrun swiftc -swift-version 5 -module-cache-path "$cache")
swift_headers="$(dirname "$(xcrun --find swiftc)")/../include/swift"
# Some CLT installs keep two identical module maps. Hide the stale copy only for this build.
if [[ -f "$swift_headers/module.modulemap" && -f "$swift_headers/bridging.modulemap" ]] &&
    cmp -s <(sed '/^\/\//d' "$swift_headers/module.modulemap") <(sed '/^\/\//d' "$swift_headers/bridging.modulemap"); then
    : > "$stage/empty.modulemap"
    "$root/.venv/bin/python" - "$swift_headers/module.modulemap" "$stage/empty.modulemap" > "$stage/overlay.json" <<'PY'
import json
import os
import sys

print(json.dumps({"version": 0, "roots": [
    {"type": "file", "name": os.path.realpath(sys.argv[1]), "external-contents": sys.argv[2]},
]}))
PY
    compiler+=(-vfsoverlay "$stage/overlay.json" -Xcc -ivfsoverlay -Xcc "$stage/overlay.json")
fi

make_app() {
    local name="$1" identifier="$2" executable="$3" agent="$4"
    mkdir -p "$stage/$name.app/Contents/MacOS" "$stage/$name.app/Contents/Resources"
    local plist="$stage/$name.app/Contents/Info.plist"
    /usr/bin/plutil -create xml1 "$plist"
    /usr/bin/plutil -insert CFBundleName -string "$name" "$plist"
    /usr/bin/plutil -insert CFBundleDisplayName -string "$name" "$plist"
    /usr/bin/plutil -insert CFBundleIdentifier -string "$identifier" "$plist"
    /usr/bin/plutil -insert CFBundleExecutable -string "$executable" "$plist"
    /usr/bin/plutil -insert CFBundlePackageType -string APPL "$plist"
    /usr/bin/plutil -insert CFBundleVersion -string 1 "$plist"
    /usr/bin/plutil -insert LSUIElement -bool "$agent" "$plist"
}

make_app "WowAgent Controller" io.wowagent.controller WowAgentController NO
make_app "WowAgent Keyboard Mouse" io.wowagent.input WowAgentInput YES
controller="$stage/WowAgent Controller.app"
helper="$stage/WowAgent Keyboard Mouse.app"
printf '%s' "$root" > "$controller/Contents/Resources/project-root"
printf '%s' "$root" > "$helper/Contents/Resources/project-root"
"${compiler[@]}" "$root/macos/IPC.swift" "$root/macos/Controller.swift" \
    -o "$controller/Contents/MacOS/WowAgentController"
/usr/bin/codesign --force --sign "$identity" "$controller"
controller_hash="$(/usr/bin/codesign -d --verbose=4 "$controller" 2>&1 | sed -n 's/^CDHash=//p')"
if [[ ! "$controller_hash" =~ ^[0-9a-f]{40}$ ]]; then
    echo "无法读取主程序的签名哈希" >&2
    exit 1
fi
printf 'identifier "io.wowagent.controller" and cdhash H"%s"' "$controller_hash" \
    > "$helper/Contents/Resources/controller.requirement"
"${compiler[@]}" "$root/macos/IPC.swift" "$root/macos/KeyboardMouse.swift" \
    -o "$helper/Contents/MacOS/WowAgentInput"
/usr/bin/codesign --force --sign "$identity" "$helper"
for app in "$controller" "$helper"; do
    /usr/bin/codesign --verify --strict "$app"
done
for app in "$controller" "$helper"; do
    destination="$output/$(basename "$app")"
    rm -rf "$destination"
    mv "$app" "$destination"
    printf '%s\n' "$destination"
done
