#!/bin/zsh
set -eu
quest_project_root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$quest_project_root"
quest_calibration_output="data/calibration/capture-$(date +%Y%m%d-%H%M%S)"
echo "这是只读标定采样，不会启动任务或发送游戏按键。"
echo "请暂停任务，切到保存的俯视角，落地、下马、脱战。"
echo "8 秒后开始。请在同一小片平地手动移动到 10 个不同位置，每处停稳约 2 秒。"
echo "前 6 点围住拟绕行范围，接着 3 点和最后参考点在其内部；不要只沿直线移动。"
echo "画面遮挡不要求看见脚底；采样后另行复核地面和角色投影锚点。"
echo "采集目录：$quest_calibration_output"
.venv/bin/python -m bot.projection_cli record --output "$quest_calibration_output" --floor-id candidate-local-plane --delay 8
echo "采样已保存，尚未认证地面或启用自动移动。请把本次采集目录发给我继续复核。"
read -r "?按回车关闭。"
