# wow-agnet — 魔兽世界正式服自动化系统

单账号「采集 / 钓鱼 / 拍卖行售卖」自动化。架构：**游戏内插件像素状态通道 + Python 截图识别 + 键鼠模拟**，不读内存、不注入。详细设计见 [PLAN.md](PLAN.md)，上线步骤见 [docs/SETUP.md](docs/SETUP.md)。

> ⚠️ 本项目属于暴雪用户协议禁止的自动化操作，封号风险无法消除。**全程使用月卡小号，主号零参与。**

采集赚金、任务自动清理和大秘境的分层与路线见 [项目架构规划](docs/architecture.md)。本地模型部署和合成样例见 [StartLux 前置实验](docs/startlux-poc.md)。

任务模块支持查看战役、主线候选和支线候选，并执行已配置的同地图路线。[任务分类与执行手册](docs/quest.md)

战斗模块提供按客户端构建号分库的技能和天赋查询、角色快照、中文技能宏与单目标试跑入口。[战斗数据与操作手册](docs/combat.md)

## 目录结构

```
addon/WowAgent/     游戏内插件（Lua）：状态采集 + 像素通道 + 调试 HUD
bot/
  core/             配置 / 状态机 / 事件总线 / 路径
  perception/       通道协议(spec) / 解码 / 截图 / OpenCV / 音频
  control/          输入后端(mac/win/mock) / 紧急停止 / 导航算法
  modules/          钓鱼 / 采集 / 售卖 / 循环调度 / 任务 状态机
  data/             SQLite / SavedVariables 解析 / 报表
  ops/              通知(Bark/ServerChan) / 游戏进程守护
  cli.py            命令行入口        selftest.py  合成环境端到端自测
config/             bot.yaml / items.yaml / routes/*.yaml
templates/12.x/     模板图片（按钮、小地图节点图标，上线时截图采集）
tests/              单元和集成测试
```

## 快速开始

```bash
# 1. 环境（macOS，已装 uv；Windows 用 python -m venv + pip install -e .）
export PATH="$HOME/.local/bin:$PATH"
uv sync

# 2. 默认配置 + 无游戏自测（应显示 PASS）
uv run python -m bot.cli init-config
uv run python -m bot.cli selftest

# 3. 全部测试
uv run pytest
```

## 上线顺序（真机 PoC → 逐模块开启）

按 [docs/SETUP.md](docs/SETUP.md) 执行：装插件 → 系统权限 → `calibrate` 校准 → 跑 `docs/poc-results.md` 里的 7 项 PoC → `probe-channel` → 钓鱼 `run --module fisher --dry-run` → 真跑。常用命令：

```bash
uv run python -m bot.cli probe-channel --duration 30     # 像素通道解码探针
uv run python -m bot.cli probe-audio --device N          # 上钩音效能量探针
uv run python -m bot.cli calibrate-channel               # 扫描全部显示器并自动校准主色条
uv run python -m bot.cli calibrate                       # 手动采集鱼漂和小地图区域
uv run python -m bot.cli learn-spell                     # 采集/钓鱼法术编号学习
uv run python -m bot.cli record-route --name my_route    # 录制采集/回城路线
uv run python -m bot.cli run --module fisher --max-duration 600 --dry-run
uv run python -m bot.cli run --module fisher --max-duration 600
uv run python -m bot.cli run --module loop --primary fisher --max-duration 86400
uv run python -m bot.cli report --hours 24               # 收益/会话报告
```

**紧急停止**：默认 `Ctrl+Alt+F12`（config 可改），触发即释放全部按键并停机推送。

## 游戏内插件（WowAgent）

1. 把 `addon/WowAgent` 整个文件夹复制到 `World of Warcraft/_retail_/Interface/AddOns/`
2. 进游戏角色选择界面勾选「加载过期插件」（Interface 版本号不匹配时会显示过期）
3. `/wagent unlock` 拖动左下角色条到无遮挡位置，再 `/wagent lock`
4. `/wagent debug` 打开人类可读 HUD 对拍；`/wagent learn on` 学习采集/钓鱼法术编号后填入 `State.lua` 的 `TRACKED_SPELLS`

## 关键设计

- **像素通道**（`bot/perception/channel_spec.py` ↔ `addon/WowAgent/*.lua`，两边同源同步）：位置 7Hz / 朝向 6Hz / 背包摘要与事件 2Hz / 状态 2Hz / 金币与分区慢轮播。位置精度约 1/1023 地图比例，误码校验 SYNC+checksum。
- **定位闭环导航**：waypoint + 朝向反馈转向（比例控制），不依赖 navmesh；卡死检测→后退/跳跃恢复→炉石兜底。
- **售卖由插件主导**：定价全部交给 Auctionator 规则，外部只点按钮；上架确认走插件的 `C_AuctionHouse.PostItem` 观察 hook（FASTEV 事件），不 OCR 价格。
- **合成环境自测**：`selftest.py` 用 Python 参考编码器模拟插件渲染、模拟世界响应输入，跑通完整钓鱼状态机——不进游戏也能回归验证整条链路。
