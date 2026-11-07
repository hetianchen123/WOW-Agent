#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
apps="$PWD/data/apps"
log="$(mktemp /private/tmp/wowagent-input-smoke.XXXXXX)"
"$apps/WowAgent Keyboard Mouse.app/Contents/MacOS/WowAgentInput" --smoke > "$log" 2>&1 &
helper=$!
trap 'kill "$helper" 2>/dev/null || true; wait "$helper" 2>/dev/null || true; rm -f "$log"' EXIT
for ((i=0; i<100; i++)); do
    if rg -q 'SIMULATION READY' "$log"; then break; fi
    if ! kill -0 "$helper" 2>/dev/null; then cat "$log"; exit 1; fi
    sleep .1
done
rg -q 'SIMULATION READY' "$log"
.venv/bin/python - <<'PY'
import json
import os
import socket

with socket.socket(socket.AF_UNIX) as client:
    client.settimeout(2)
    client.connect(f"/private/tmp/wowagent-input-{os.getuid()}/simulate.sock")
    with client.makefile("rb") as stream:
        response = json.loads(stream.readline())
        assert response["ok"] is False and "签名" in response["error"], response
print("Unsigned socket client rejected")
PY
"$apps/WowAgent Controller.app/Contents/MacOS/WowAgentController" --smoke
