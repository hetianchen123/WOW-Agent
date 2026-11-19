#!/bin/zsh
set -eu
if [[ $# == 1 && "$1" == --prepared ]]; then
    quest_project_root="$(cd "$(dirname "$0")/.." && pwd -P)"
    apps="$quest_project_root/data/apps"
    controller="$apps/WowAgent Controller.app/Contents/MacOS/WowAgentController"
    helper="$apps/WowAgent Keyboard Mouse.app"
    if [[ ! -x "$controller" || ! -d "$helper" ]]; then
        print -u2 '找不到配套的 WowAgent App，请先运行 macos/build.sh。'
        exit 2
    fi
    /usr/bin/open -g "$helper"
    exec "$controller" --request-prepared
fi
exec "$(dirname "$0")/quest-accept.command" navigate-quest "$@"
