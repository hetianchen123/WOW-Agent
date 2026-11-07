# 上线手册（macOS · 国服正式服）

按顺序执行。每一步的验证标准写清楚了才进下一步。全程使用**月卡小号**。

## 0. 前置

- macOS（Apple 芯片），国服正式服客户端可正常登录
- 装好 uv：`curl -LsSf https://astral.sh/uv/install.sh | sh`
- 项目就绪：`uv sync && uv run python -m bot.cli init-config && uv run python -m bot.cli selftest`（selftest 必须 PASS）

## 1. 系统权限（Python/终端）

推荐用[双 App 启动方式](macos-apps.md)分别授权。以下步骤适用于直接在终端运行 CLI。

在「系统设置 → 隐私与安全性」为启动程序的 App 授权。用系统「终端」启动，就给「终端」授权。授权后完全退出并重开该 App。

- **录屏与系统录音**（部分系统显示为「屏幕录制」）供自动校准和读游戏画面使用。[Apple 录屏权限说明](https://support.apple.com/zh-cn/guide/mac-help/mchld6aa7d23/27/mac/27)
- **辅助功能**（部分系统显示为「无障碍」）供实机键鼠控制使用。只做自动校准时，开通录屏权限即可。[Apple 辅助功能权限说明](https://support.apple.com/zh-sg/guide/mac-help/mh43185/mac)
- **麦克风**用于可选的 BlackHole 音频探针。

## 2. 安装游戏内插件

1. 把 `addon/WowAgent` 文件夹复制到：
   `World of Warcraft/_retail_/Interface/AddOns/WowAgent/`
2. 登录界面 → 插件 → 勾选 WowAgent（若显示过期，勾选「加载过期插件」）
3. 进游戏后屏幕左下角出现 8 个色块 = 插件工作
4. `/wagent unlock` → 拖色条到**无 UI 遮挡**的角落 → `/wagent lock`

WowAgent 0.2.8 在游戏左上方提供 **WowAgent** 按钮。正式服也可从小地图旁的插件菜单打开。从 0.2.0 或更早版本升级时，先完全退出并重开客户端，让游戏加载新增的天赋推荐文件。0.2.1 及之后的版本升级可用 `/reload`。

- **战斗准备**用名称下拉选择专精与该专精的已保存天赋。也可配置一键输出、换装及放置坐骑、增益和补给宏。
- **天赋推荐**根据当前专精和玩法显示推荐。满级猎人在正式服 12.1.5 可选单刷 / 地下堡、团本单目标、团本多目标或大秘境，打开原生窗口确认导入。其他情况可打开游戏天赋页查看起始配置。
- **任务与色条**可从任务日志选取任务、开启追踪、读取航点和移动色条。
- **准备自动接任务**在任务与色条页，读取当前 NPC 的未接任务并准备数字键 9 操作宏。启用键鼠会话后，可双击 `macos/quest-accept.command`，运行本地 StartLux 和脚本的接任务测试。
- **任务寻路**选择已接任务及坐骑，点「准备任务寻路」。启用键鼠会话后双击 `macos/quest-navigate.command`，按飞行、地面坐骑、步行的优先级赶往任务目标。
- **动作栏布局**按格子选动作，预览后才能确认重排。面板提供恢复备份和取消等待。输入和布局草稿按角色保存。
- **清空主栏**位于战斗准备和动作栏布局页，脱战点击即可清空当前主栏页的 12 格并自动备份。可以点「恢复备份」还原。再次清空或重排前，先恢复上次布局，或在动作栏布局页点「采用布局」保留当前布局。

## 3. 校准

启动下列命令后把已登录的游戏窗口切到前台，让 WowAgent 主色条的 8 个色块保持可见。

```bash
.venv/bin/python -m bot.cli calibrate-channel
```

程序等待最多 30 秒，扫描所有显示器，通过帧校验和与交替心跳确认主色条。输出最后一行是 JSON。`calibrated` 表示已保存新坐标，`already_calibrated` 表示现有坐标已通过验证。缺权限、超时或发现多个通过验证的色条时会返回错误状态并保留当前配置。可用 `--duration 60` 延长等待，或加 `--dry-run` 只核对坐标。

换显示器位置、移动游戏窗口后重新运行即可。自动检测使用当前协议的 142×16 截图坐标布局。若改变了游戏 UI 缩放导致色块尺寸改变，恢复原缩放后再校准。任务和战斗色条以主色条为基准。

鱼漂和小地图区域用手动工具采集。

```bash
uv run python -m bot.cli calibrate
```

记录并手工填入 `config/bot.yaml`：

| 校准目标 | 按键操作 | 填入字段 |
|---|---|---|
| 鱼漂水域左上+右下 | 两次 `r` | `fishing.bobber_roi` |
| 小地图左上+右下 | 两次 `r` | `gathering.minimap_roi` |

## 4. 七项 PoC（结果记录到 docs/poc-results.md）

1. **输入注入**：`uv run python -m bot.cli run --module fisher --dry-run` 之外，手工验证：游戏内按脚本发一次钓鱼键（可临时用 calibrate 的 q 退出后跑 `run --module fisher --max-duration 30 --dry-run` 观察；真正按键验证在 PoC 6 一起做）
2. **通道解码**：`uv run python -m bot.cli probe-channel --duration 30` → 误码率 < 5%，pos/hdg 数值随移动变化
3. **朝向约定**：面朝北站立，probe-channel 里 `hdg≈0`；左转 90° 后 `hdg≈1.57`。若相反 → `navigation.facing_convention: cw_east`
4. **定位 API**：小范围走动，`pos` 连续变化；进副本应停更（预期行为）
5. **音频上钩**：装 [BlackHole](https://existential.audio/blackhole/)，`probe-audio --device N` 抛竿听上钩，ratio 峰值 ≥10 即可用；`bite_threshold` 设峰值的 60%
6. **钓鱼闭环（干跑→真跑）**：先 `run --module fisher --dry-run` 看日志决策正确；去掉 `--dry-run` 跑 30 分钟，≥95% 收竿成功
7. **法术学习**：`learn-spell` 期间手动钓鱼/采药/采矿各一次，把打印的 spellID 填入 `addon/WowAgent/State.lua` 的 `TRACKED_SPELLS`（钓鱼=1 采药=2 采矿=3）

## 5. 模板采集（采集/售卖模块用）

游戏内截图（⌘⇧4）裁剪保存到 `templates/12.x/`：

- `node_herb.png` / `node_ore.png` / `node_pool.png`：小地图追踪图标（各采一个）
- `ah_window.png`：拍卖行界面特征区
- `post_button.png`：Auctionator 上架按钮（未置灰状态）
- `mail_frame.png` / `open_all_mail.png`：邮箱界面与「打开所有」按钮

Auctionator 设置：售卖页配置好分组、底价（最低价保护）、上架数量。**机器永不点击购买类按钮**。

## 6. 路线录制

```bash
# 采集路线（骑坐骑沿路线飞一圈）
uv run python -m bot.cli record-route --name gather_route --mode fly
# 枢纽城内：炉石点→拍卖师→邮箱→出城点（短路线，逐段录）
uv run python -m bot.cli record-route --name hub_to_ah --mode ground
uv run python -m bot.cli record-route --name ah_to_mail --mode ground
uv run python -m bot.cli record-route --name hub_to_spot --mode ground
```

录制时按 `s` 开始/暂停、`q` 保存。检查生成的 yaml 点距均匀、无穿墙。

## 7. 逐模块上线

```bash
# 采集（先短后长）
uv run python -m bot.cli run --module gatherer --route gather_route --max-duration 600 --dry-run
uv run python -m bot.cli run --module gatherer --route gather_route --max-duration 1800

# 售卖（站在炉石点附近，背包放白名单物品）
uv run python -m bot.cli run --module seller --max-duration 900

# 24h 循环（钓鱼主导，夜间自动切钓鱼）
uv run python -m bot.cli run --module loop --primary fisher --max-duration 86400
uv run python -m bot.cli report --hours 24
```

## 8. 推送通知（可选）

iOS 装 Bark，把 key 填 `config/bot.yaml` → `notify: {provider: bark, key: ...}`；掉线/卡死升级/停机都会推送。

## 故障排查

| 现象 | 处理 |
|---|---|
| probe-channel 全 bad | 确认色条可见，再运行 `calibrate-channel` |
| 游戏不响应按键 | 权限未生效（重启终端）；后台跑时游戏必须前台 |
| 转向越转越偏 | `input.invert_turn: true`；或朝向约定改 `cw_east` |
| 上钩漏检 | 降低 `bite_threshold`；确认 bobber_roi 覆盖鱼漂落点 |
| 补丁后全乱 | 重跑校准+重截模板（这是预期维护成本，~30 分钟） |
