# StartLux 本地决策前置实验

用合成状态验证 `状态 JSON → 候选技能 → 选项概率 → 建议或弃权`。脚本直接请求本机 HTTP 服务，输出 JSON 报告。实验覆盖采集赚金、任务清理和大秘境回放，全部使用合成输入。

## 模型与运行方式

官方 v1 和 v1.1 的对应规格均为 9B。本实验采用 `StartLux-Decision-v1.1-9B-Instant-MLX-4bit`，官方权重约 5.96 GB。本机为 Apple M6、24 GB 统一内存。官方 StartLux 引擎负责双向注意力和选项读出。模型权重以 CC BY-NC 4.0 发布，本次用于离线研究。[官方模型卡](https://huggingface.co/startlux-models/StartLux-Decision-v1.1-9B-Instant-MLX-4bit) · [官方推理引擎](https://github.com/StartLuxLabs/StartLux-Decision)

在项目根目录执行。当前机器的文件放在 `data/startlux/`，仓库忽略该目录。已安装的机器可直接从启动服务开始。

```bash
# 仅首次安装
slx="$PWD/data/startlux"
engine_rev=a99977d9332decc9fb27a2488b4a83b533b6cd5f
model_rev=df5276ab9c9bf51f1c63dcd06f6645cbb9aad282
mkdir -p "$slx"
export UV_CACHE_DIR="$slx/uv-cache"
uv venv --python 3.12 "$slx/.venv"
curl -fSL "https://codeload.github.com/StartLuxLabs/StartLux-Decision/tar.gz/$engine_rev" -o "$slx/engine.tar.gz"
tar -xzf "$slx/engine.tar.gz" -C "$slx"
uv pip install --python "$slx/.venv/bin/python" \
  "${slx}/StartLux-Decision-${engine_rev}[mlx]" "mlx==0.32.4" "mlx-vlm==0.7.12" "transformers==5.22.1"
"$slx/.venv/bin/hf" download startlux-models/StartLux-Decision-v1.1-9B-Instant-MLX-4bit \
  --revision "$model_rev" --local-dir "$slx/model"
```

```bash
# 终端 1，在项目根目录启动，Ctrl+C 停止
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  data/startlux/.venv/bin/startlux-decide serve --backend mlx \
  --model data/startlux/model --name StartLux-Decision-v1.1-9B-Instant-MLX-4bit \
  --host 127.0.0.1 --port 8090

# 终端 2，待服务就绪后运行
.venv/bin/python experiments/startlux/run.py
```

## 评估

- 共 6 组对照、12 个独立状态。每组共享一套候选和规则，改变关键状态后预期动作翻转。每个状态按两种候选顺序各运行一次，共 24 次真实 HTTP 请求。
- 固定优先级基线每组始终选择预先排第一的候选，候选展示顺序反转时基线不变。对照的严格通过条件是两个状态在两种顺序下均答对。这个基线用来验证模型是否根据状态改选。后续需要以业务规则为基线，在带噪声的任务回放上比较。
- 记录原始 Top-1、改序稳定性、置信度、弃权和端到端延迟。默认弃权线为置信度低于 0.55 或无合适选项概率达到 0.20。过期状态样例在发请求前被拦截，2.5 秒是本实验的演示值。阈值未做业务校准。
- 第一次请求的耗时单独记录，其余 23 次计算 p50 和 p95。服务启动和加载时间单独记录。详细结果默认写到 `data/startlux/results.json`。

## 2026-11-06 本机结果

| 指标 | 结果 |
| --- | --- |
| 服务启动到 `/health` 就绪 | 10.41 秒，官方引擎确认 MLX 4-bit 和双向注意力 |
| 首次 HTTP 请求 | 455.9 ms |
| 后续 HTTP 请求 | p50 181.9 ms，p95 189.5 ms |
| 固定优先级基线 | 12/24 |
| 模型原始 Top-1 | 22/24。采集 8/8，任务 8/8，大秘境回放 6/8 |
| 对照组严格通过 | 5/6 |
| 候选改序后选择一致 | 12/12 个状态，包含唯一误判状态 |
| 应用弃权规则后 | 22 次给出建议且都符合预期，2 次弃权 |
| 过期状态样例 | HTTP 调用前弃权 |

唯一误判发生在模拟敌方部队进度 94%、还可补 7% 的场景。两种候选顺序下，模型都选了跳过可选敌方队伍，置信度分别为 0.212 和 0.337，均触发弃权。把结算所需进度编码为业务规则，在生成候选时筛掉无法满足进度要求的路线。剩余的可行路线仍可交给模型排序。

这批请求使用短文本和显式规则。结果验证了本机部署、候选选择和弃权流程。复杂任务链、感知误差和客户端同机运行时的延迟仍需独立测量。原始报告、启动信息和日志位于 `data/startlux/`。测试结束后停止了模型服务，权重和运行环境可直接复用。
