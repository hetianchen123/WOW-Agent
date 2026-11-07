# macOS 双 App 启动

在项目根目录运行 `bash macos/build.sh` 生成两个本地 App。主程序使用这个工作区的 `.venv`、脚本和配置。两个 App 已固定在当前工作区，移动项目后重新构建。

| App | macOS 权限 | 用途 |
| --- | --- | --- |
| `data/apps/WowAgent Controller.app` | 录屏与系统录音 | 启动项目 CLI、读取色条、显示输出 |
| `data/apps/WowAgent Keyboard Mouse.app` | 辅助功能 | 发送受约束的键鼠事件 |

在「系统设置 → 隐私与安全性」打开对应项目。列表里没有 App 时，点「＋」，在文件选择器中用 `⌘⇧G` 输入该 App 的完整路径。授权后退出并重开对应 App。使用本方式运行时，可以关闭 ChatGPT。StartLux 仍可作为单独启动的本地服务。

本机 macOS 27.2 的权限页显示为「设备控制和数据访问」。键鼠 App 的「辅助功能」提示对应这个页面。若开关已打开但 App 自检仍显示未授权，先退出并重开键鼠 App。仍未识别时，可在当前权限页移除旧的键鼠 App 条目，再添加本项目 `data/apps/WowAgent Keyboard Mouse.app` 并开启。

1. 双击主 App，点「检查录屏权限」。保留默认的 `calibrate-channel`，点「运行」后切回魔兽，等待校准结果。
2. 实机输入前打开键鼠 App。在菜单栏「WoW 键鼠」里选择「检查键鼠权限」。如果辅助功能已开启而事件发送仍显示未授权，可以在同一菜单里选择「请求发送事件权限」并按系统提示操作。
3. 点「启用一次会话」，从主 App 运行已有的 CLI 参数，例如 `run --module quest --quest-plan "路径" --max-duration 60`。脚本会自动切回游戏，若系统未切换，在 12 秒内手动切回。`--dry-run` 使用模拟输入。
4. 点主 App 的「停止」或菜单栏的「立即停止」可中止。游戏失焦、脚本断连，或按键仍被按住而脚本停止回应时，键鼠 App 会释放本会话输入。下一次执行需要重新启用。

键鼠 App 只接收来自本次配套签名的主 App 的本地连接。它验证前台客户端、允许的按键和鼠标目标所在窗口。辅助功能授权的系统范围仍取决于 macOS。当前用本机临时签名构建，修改后重新构建时可能需要重新授权。[Apple 代码签名与权限说明](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)

自动接任务可在游戏里点「准备自动接任务」，启用键鼠会话后双击 `macos/quest-accept.command`。启动器加载本地 StartLux 并启动主 App 的 `accept-quest` 命令。关闭本次主控窗口后，启动器会结束自己启动的模型服务。完整操作见 [任务模块](quest.md)。

`bash macos/smoke.sh` 使用模拟执行器验证签名、输入通信、窗口边界、失焦和按键释放。
