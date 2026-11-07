# Qwen3-VL 本地寻路视觉实验

实验用之前保存的两张魔兽截图，对比整张窗口和裁去大部分界面的场景画面。脚本输出前、左、右三个方向的可通行判断、可见依据和建议观察方向。输出只写入报告。

模型是 `mlx-community/Qwen3-VL-8B-Instruct-4bit`，本次下载的修订为 `defcdea7cc7a4b0858fea563cbbce171d328e457`，本地目录为 `data/qwen3-vl/model`。脚本使用项目已有的 MLX 0.32.4 和 MLX-VLM 0.7.12 推理环境。模型加载和推理使用本地文件，并启用离线模式。 [Qwen 官方模型卡](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct) · [MLX 量化权重](https://huggingface.co/mlx-community/Qwen3-VL-8B-Instruct-4bit)

在项目根目录运行以下命令。裁剪坐标是相对于整张截图的左、上、右、下边界，适用于这两张截图的界面布局。

```sh
data/startlux/.venv/bin/python experiments/qwen3_vl/run.py --repeat 2 \
  data/qwen3-vl/samples/wooden-staircase.png data/qwen3-vl/samples/gate-road.png

data/startlux/.venv/bin/python experiments/qwen3_vl/run.py --repeat 2 \
  --crop .42 .10 .68 .635 \
  data/qwen3-vl/samples/wooden-staircase.png data/qwen3-vl/samples/gate-road.png
```

模型需要重新下载时可运行以下命令。

```sh
HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
  data/startlux/.venv/bin/hf download mlx-community/Qwen3-VL-8B-Instruct-4bit \
  --revision defcdea7cc7a4b0858fea563cbbce171d328e457 \
  --local-dir data/qwen3-vl/model
```

整图实验中，模型识别到了木楼梯阻挡，但把城门道路也误判为受阻。裁剪后，模型在楼梯场景建议先观察，在城门场景确认前方能看到连续地面。报告保存在 `data/qwen3-vl/results.json` 和 `data/qwen3-vl/results-cropped.json`。这组样本只验证截图识别，左右是否实际可通行仍需用其他视角和移动反馈核对。

## 联合决策探查

启动本地 StartLux 服务后，可用以下命令检查 Qwen 视觉结果和 StartLux 的候选选择。

```sh
.venv/bin/python -m bot.cli terrain-probe --image data/qwen3-vl/samples/wooden-staircase.png
.venv/bin/python -m bot.cli terrain-probe --image data/qwen3-vl/samples/gate-road.png
```

不带 `--image` 时，探查读取当前魔兽画面和任务寻路色条。角色应下马脱战，先准备任务寻路，再关闭面板留出场景画面。需要验证输入时，启用一次键鼠会话并在 WowAgent 主控中运行 `terrain-probe --execute`。

探查先逐张看图。累计两次没有找到通路或短步没有确认位移时，才在原地采集前、左、背后和右四个朝向。Qwen 一次加载后依次处理四张画面，StartLux 只在正前方可走、且未试走失败的视角中做选择。脚本转到所选方向后再拍一张图核对，最后只试走一短步。前台丢失、进入战斗、转向未核对或角色位置变化时，本轮多视角探查会停止。
