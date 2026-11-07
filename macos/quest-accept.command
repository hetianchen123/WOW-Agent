#!/bin/zsh
set -eu
quest_project_root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$quest_project_root"
exec .venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from bot.decision.startlux import NoRedirect

root = Path.cwd()
opener = build_opener(ProxyHandler({}), NoRedirect())
server = None
log_file = None

def ready():
    try:
        with opener.open("http://127.0.0.1:8090/health", timeout=1) as response:
            health = json.load(response)
    except URLError:
        return False
    if not str(health.get("model", "")).startswith("StartLux-Decision"):
        raise ValueError("本地 8090 端口的服务不是 StartLux")
    return health.get("status") == "ok"

try:
    if not ready():
        print("正在加载本地 StartLux 模型。", flush=True)
        log_file = (root / "data/startlux/server.log").open("a")
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
               "HF_HUB_DISABLE_TELEMETRY": "1"}
        server = subprocess.Popen([
            str(root / "data/startlux/.venv/bin/startlux-decide"),
            "serve", "--backend", "mlx", "--model", str(root / "data/startlux/model"),
            "--name", "StartLux-Decision-v1.1-9B-Instant-MLX-4bit",
            "--host", "127.0.0.1", "--port", "8090",
        ], stdout=log_file, stderr=log_file, env=env)
        deadline = time.monotonic() + 45
        while not ready():
            if server.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("模型服务启动失败，请查看 data/startlux/server.log")
            time.sleep(.5)
    print("本地模型已就绪。测试完成后关闭主控窗口，即可结束本次启动的模型服务。", flush=True)
    print("主控启动后会自动切回游戏。请保持已准备的 NPC 任务对话打开。", flush=True)
    subprocess.run([
        "open", "-n", "-W", str(root / "data/apps/WowAgent Controller.app"),
        "--args", "accept-quest",
    ], check=True)
except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
    raise SystemExit(str(exc))
finally:
    if server is not None and server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    if log_file is not None:
        log_file.close()
PY
