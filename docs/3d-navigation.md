# 三维地面导航 v1

核对与实现日期：2026-11-18。**已实现、离线验证；尚未在正式服实走。**

本版提供真实 Recast/Detour 后端、碰撞场景导入、三维路径查询、离线预览，以及输出现有 `TravelCommand` 的位置反馈跟随器。支持分层地面，不把地面网格称为自由三维飞行导航。

## 开源调研与选择

| 项目 | 能力 | 本项目采用方式 |
| --- | --- | --- |
| [TrinityCore mmaps_generator](https://github.com/TrinityCore/TrinityCore/tree/master/src/tools/mmaps_generator) | 魔兽地图／模型提取后生成移动网格；分块、坡度、水域、特殊连接 | 参考资源提取与导航计算分离的架构；未复制 GPL 代码，也未声称能直接用于当前国服 |
| [OpenMW DetourNavigator](https://github.com/OpenMW/openmw/tree/master/components/detournavigator) | 开放世界碰撞导航；[调试显示、缓存、角色尺寸与分块参数](https://openmw.readthedocs.io/en/stable/reference/modding/settings/navigator.html) | 参考源碰撞、可走表面与路线分层显示，以及角色尺寸约束 |
| [Recast / Detour](https://github.com/recastnavigation/recastnavigation/tree/v1.6.0) | 三角网格体素化、净空／坡度／半径过滤、连通区域、路径查询 | 直接使用 1.6.0 的 Recast 和 Detour C++ 源码，保留 zlib 许可 |
| [wow.export](https://github.com/Kruithne/wow.export) | 正式服 CASC／CDN 资源读取，ADT、WMO、M2 预览与导出 | 候选资源前端；本版输入为明确装配、明确碰撞语义的三角 OBJ 场景 |
| [CascLib](https://github.com/ladislav-zezula/CascLib) | 读取暴雪 CASC 存储 | 后续直接资源导入的候选，不是当前依赖 |

Recast 固定源码提交 `6dc1667f580357e8a2154c28b7867bea7e8ad3a7`。收录的两库源码未修改，约 604 KB；归属和许可见 [`native/nav3d/README.md`](../native/nav3d/README.md)。编译不需要在线下载。

**导出工具边界**：本次检查 wow.export 提交 `81cc25e4c500d2153c503b381eba82a906f52ba8` 的 [ADTExporter](https://github.com/Kruithne/wow.export/blob/81cc25e4c500d2153c503b381eba82a906f52ba8/src/js/3D/exporters/ADTExporter.js) 和 [WMOExporter](https://github.com/Kruithne/wow.export/blob/81cc25e4c500d2153c503b381eba82a906f52ba8/src/js/3D/exporters/WMOExporter.js)：模型摆放另写 CSV，包含位置、旋转、缩放、WMO doodad 集合；不能把地形 OBJ 当成包含全部建筑的场景。普通 WMO 渲染导出不能自动当作碰撞网格。外部导出流程可能记录并跳过失败物件；导入导航前必须对账，不能将缺失障碍当作空地。

## 能力与边界

| 能力 | 状态 |
| --- | --- |
| 多 OBJ 装配、仿射变换、反射时修正三角面绕序 | 已实现，离线通过 |
| 半径、身高、坡度、台阶高度、头顶净空约束 | 已实现，离线通过；参数尚未按当前角色实机标定 |
| 绕墙、门洞、坡道和楼梯到上层 | 合成碰撞场景通过 |
| 相同 XY 的上下层不会无连接跳转 | 合成场景通过 |
| 临时障碍 AABB、阻断后重新查询 | 已实现；保守禁用整个相交多边形，可能过度封路，不是精确动态重烘焙 |
| 完整路径、端点高度、查询容量耗尽检查 | 已实现；拒绝部分路径，不伪报到达 |
| 位置反馈跟随、旧帧等待、偏离／卡住／场景变更停止 | 已实现，模拟通过；未接入自动任务默认入口 |
| 无外部依赖的可旋转 HTML 预览 | 已生成；浏览器工具本地 URL 策略阻止实屏验看，未声称交互 UI 实测通过 |
| 当前国服 CASC 自动提取、碰撞标记解析、完整摆放 | **尚未实现**；不是只给安装目录就能运行 |
| 原生 XYZ／世界地图／相位遥测与当前场景对齐 | **待接入与实机验证**；当前二维任务色条不提供完整契约 |
| 跨区分块流式导航、任务相位自动选择、门状态识别 | 待实现 |
| 电梯／传送门／船／跳跃等特殊连接 | 待实现；当前无连接时返回不可达 |
| 飞行、游泳、坐骑不同体型、战斗避险 | 未纳入 v1；只处理一个地面角色配置 |

本机 `.build.info` 只读核对：正式服 `wow` 为 **12.1.5.70961**，另有 `wowf` **1.60.1.70938**。二者不可混用；旧 API 文档中的 70783 不作为本版资源构建号。本次没有读取游戏进程内存、安装／重载插件或发送游戏按键。

## 运行

在项目根目录执行，第一次使用本机 C++ 编译器构建共享库：

```sh
.venv/bin/python -m bot.nav3d_cli backend
.venv/bin/python -m bot.nav3d_cli demo --output data/nav3d/my-first-demo
```

输出目录必须不存在，防止覆盖已有验证。生成八套 `scene.json`、`collision.obj`、`mesh/manifest.json`、`mesh/mesh.bin`、`query.json` 和 `path.json`，以及总览 `preview.html` 和 `results.json`。在浏览器打开 `preview.html`，可拖动旋转、滚轮缩放、切换场景／顶视／侧视、分别隐藏碰撞几何、可走表面和路线。

已生成的本次结果目录：`data/nav3d/release-v1/`。

自有碰撞场景：

```sh
.venv/bin/python -m bot.nav3d_cli build path/to/scene.json data/nav3d/my-region
.venv/bin/python -m bot.nav3d_cli query data/nav3d/my-region path/to/query.json --output data/nav3d/my-path.json
.venv/bin/python -m bot.nav3d_cli preview path/to/scene.json data/nav3d/my-region --path data/nav3d/my-path.json --output data/nav3d/my-preview.html
```

失败返回退出码 2，在 stderr 写 `status=rejected` 与具体原因；查询失败不生成路线。地图与查询输出不覆盖已有文件。所有路径输出 `reached_in_game=false`。

## 输入契约

场景示例：

```json
{
  "schema": 1,
  "geometry_kind": "collision",
  "units": "yards",
  "up_axis": "Z",
  "frame": {
    "client_build": "12.1.5.70961",
    "world_id": 0,
    "phase_id": "replace-with-actual-scene-variant",
    "coordinate_system": "east_north_up"
  },
  "evidence": "unverified_collision_export",
  "agent": {
    "cell_size": 0.2, "cell_height": 0.1,
    "height": 2.0, "radius": 0.4,
    "max_climb": 0.4, "max_slope": 45.0
  },
  "instances": [{
    "path": "assembled_collision.obj",
    "transform": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
  }]
}
```

`world_id=0` 仅为格式示例，不能套用到当前区域。它是世界／实例地图身份，**不是 uiMapID**。`phase_id` 必须标明场景变体；本版由调用方提供，不会自动识别。坐标系不从文件名猜测。

每个 OBJ 只接受三角面；正负索引与 `v/vt/vn` 引用可用。凹多边形须先由可靠工具三角化，不能用简单扇形剖分意外封门或跨墙。每个 4×4 仿射矩阵把模型本地坐标变换到同一 Z 向上、以码为单位的场景；缺失资产／无效变换直接失败。其他轴系必须显式变换，**不能默认 OBJ 等于 UnitPosition**。

查询示例（坐标必须替换为真实场景中的 XYZ）：

```json
{
  "frame": {
    "client_build": "12.1.5.70961", "world_id": 0,
    "phase_id": "replace-with-actual-scene-variant",
    "coordinate_system": "east_north_up"
  },
  "start": [3, 8.5, 0],
  "goal": [21, 8.5, 0],
  "horizontal_snap": 0.5,
  "vertical_snap": 0.3,
  "spacing": 0.25,
  "blocked_boxes": []
}
```

缺少高度不能用 0 补齐。默认最多允许端点水平偏移 0.5 码、垂直偏移 0.3 码；悬在两层之间或落在墙里的终点返回失败。临时障碍格式为 `[[minXYZ,maxXYZ], ...]`，须包含真实高度；查询会计入角色半径和站立高度，避免跨层封路。障碍变化后重新规划，不追原目的地方向。

## 后端与缓存

入口 `bot/nav3d_cli.py`；Python 接口 `bot.nav3d.NavMesh` / `Query`；碰撞导入 `bot/nav3d/geometry.py`；本机桥接 `native/nav3d/bridge.cpp`。

构建执行体素化、表面过滤、半径侵蚀、区域划分、多边形与细节高度生成。查询保留多边形边界并按细节表面采样高度，不把楼梯路径压成空间直线。边界浮点误差只允许在原多边形做不超过 0.001 码的水平校正，不搜另一层补高度。

每张图是一个局部区域，最多四百万水平体素格、两百万顶点／三角面。大世界坐标先平移到局部原点再转 float32；返回路点恢复世界坐标。大区域须先拆分，本版没有跨块拼接。

缓存保存资源 SHA-256、变换、几何与网格摘要、角色参数、世界身份、后端源码版本。二进制仅作本机生成缓存；后端源码／架构变化必须重建，不作为第三方任意二进制导入格式。

## 运行时接入

`WorldRouteFollower` 产生现有 `TravelCommand`，不创建输入后端。调用方须先完成世界坐标对齐，再传入 `WorldPose`：同坐标系 XYZ、角色朝向、序列、单调时钟时间戳、地面状态、键位核对和战斗状态。朝向约定为 `east_north_up` 坐标下的 `GetPlayerFacing`（北为 0，西为 π/2）。

要求显式 `alignment_verified=True` 和匹配的网格／动态障碍版本。旧帧释放输入等新帧；超时、失配、异常位移、离地、战斗、取消或连续无进展停止。同向转向至少间隔 0.45 秒、反向至少 0.9 秒，前进为有界短脉冲。

`nav3d_route_arrived` 只表示观测到路线终点，不表示任务完成、NPC 进入互动范围或真实门已打开。任务执行器仍须核对原生回执。本版没有改变 `QuestTrip` 默认选择，也没有把二维色条转换成虚构 XYZ。

实机接入下一步：提取当前建筑及周边真实碰撞数据，核对导出完整性和轴系，补齐只读原生 XYZ／世界身份遥测，在多个独立位置验证对应关系，最后沿现有授权、焦点和急停执行器做短段实走。不要求用户手动跑标定点。

## 验证证据

最终回归：**49 项三维导航专项测试通过；全量 1217 项 Python 测试通过（62.12 秒）**。测试 XML、场景结果、版本证据、交互 HTML 和已目视检查的静态几何图保存在 `data/nav3d/release-v1/`。HTML 的 JavaScript 语法检查通过，浏览器交互验看仍受前述限制。

`tests/test_nav3d.py` 使用实际编译的 Recast/Detour，不以模拟 A* 替代库调用。涵盖墙体净距、门框、坡道／台阶高度、无连接楼层、陡坡、净空、窄缝、动态障碍、身份失配、错误端点、缓存污染、反射、大坐标、CLI 和跟随器停止条件。

```sh
.venv/bin/python -m pytest -o addopts= -q tests/test_nav3d.py
```

示例结果：绕墙约 22.41 码、门洞 18.00 码、坡道约 21.57 码、楼梯约 21.60 码；另外四个不可达场景明确拒绝。这些是**合成几何与模拟反馈**，不是客户端资源提取成功或正式服实走成功。
