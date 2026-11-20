import AppKit
import ApplicationServices
import Darwin
import Security

private let gameID = "com.blizzard.worldofwarcraft"

private final class KeyboardMouse: NSObject, NSApplicationDelegate {
    let simulate: Bool
    private var server: Int32 = -1
    private var item: NSStatusItem?
    private let state = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private var armed = false
    private var session = false
    private var keepOnline = false
    private var focusEpoch = 0
    private var waitingForForeground = false
    private var game: pid_t?
    private var lastContact = ProcessInfo.processInfo.systemUptime
    private var keys = Set<CGKeyCode>()
    private var navigationPulse: (key: CGKeyCode, token: UUID)?
    private var buttons = Set<String>()
    private var point: CGPoint?
    private var eventCount = 0
    private var simulatedFocus = true
    private var timer: Timer?
    private var preparingLaunchLog = false
    private var preparationCancelled = false
    private var launchDeadline: TimeInterval?
    private var nextLaunchCheck: TimeInterval = 0
    private var launchWait: LaunchWait?
    private var questRequestedAt: Date?
    private var questProcess: Process?

    private enum LaunchWait: String {
        case accessibility = "请授权当前版键鼠助手（设备控制和数据访问）"
        case postEvents = "等待键鼠事件发送权限"
    }

    init(simulate: Bool) { self.simulate = simulate }

    private static let keyCodes: [String: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
        "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
        "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25,
        "7": 26, "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
        "j": 38, "k": 40, "n": 45, "m": 46, "tab": 48, "space": 49, "esc": 53,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
        "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
        "left": 123, "right": 124, "down": 125, "up": 126,
    ]
    private static let pulseKeys: Set<String> = ["w", "s", "q", "e", "left", "right"]

    func applicationDidFinishLaunching(_ notification: Notification) {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = simulate ? "WoW 输入模拟" : "WoW 键鼠"
        let menu = NSMenu()
        menu.addItem(state)
        for (title, action) in [("启动任务主控", #selector(runPrepared)),
                                ("查看任务运行日志", #selector(showQuestLog)),
                                ("启用一次会话", #selector(arm)), ("立即停止", #selector(cancel)),
                                ("检查键鼠权限", #selector(permissions)),
                                ("请求发送事件权限", #selector(requestPostPermission)),
                                ("退出", #selector(quit))] {
            let row = menu.addItem(withTitle: title, action: action, keyEquivalent: "")
            row.target = self
        }
        item.menu = menu
        self.item = item
        do { try start() }
        catch { state.title = String(describing: error); return }
        updateStatus()
    }

    func start() throws {
        guard let file = Bundle.main.url(forResource: "controller", withExtension: "requirement") else {
            throw InputError.message("找不到配套主程序的签名")
        }
        let text = try String(contentsOf: file, encoding: .utf8)
        var requirement: SecRequirement?
        guard SecRequirementCreateWithString(text as CFString, SecCSFlags(), &requirement) == errSecSuccess,
              let requirement else { throw InputError.message("主程序签名配置无效") }
        let path = try InputSocket.path(simulate: simulate)
        let lockFD = open(path + ".lock", O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_RDWR, 0o600)
        guard lockFD >= 0 else { throw InputError.message("无法打开键鼠执行器锁") }
        guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
            close(lockFD)
            throw InputError.message("已有一个键鼠执行器正在运行")
        }
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { close(lockFD); throw InputError.message("无法创建键鼠服务") }
        InputSocket.configure(fd)
        unlink(path)
        guard (try InputSocket.withAddress(path, { Darwin.bind(fd, $0, $1) })) == 0,
              chmod(path, 0o600) == 0, listen(fd, 4) == 0 else {
            close(fd)
            close(lockFD)
            throw InputError.message("无法监听键鼠连接")
        }
        server = fd
        let timer = Timer(timeInterval: 0.10, repeats: true) { [weak self] _ in self?.watch() }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
        DispatchQueue.global().async {
            defer { close(lockFD) }
            while true {
                let client = accept(fd, nil, nil)
                if client < 0 {
                    if errno == EINTR { continue }
                    return
                }
                InputSocket.configure(client)
                if InputSocket.validateController(client, requirement: requirement) {
                    self.serve(client)
                    DispatchQueue.main.sync { if self.session { self.stop() } }
                } else {
                    _ = InputSocket.send(client, ["ok": false, "error": "连接方与配套主程序签名不匹配"])
                }
                close(client)
            }
        }
    }

    private func serve(_ fd: Int32) {
        var pending = Data()
        var bytes = [UInt8](repeating: 0, count: 512)
        while true {
            let count = recv(fd, &bytes, bytes.count, 0)
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { return }
            pending.append(contentsOf: bytes.prefix(count))
            while let newline = pending.firstIndex(of: 10) {
                guard pending.distance(from: pending.startIndex, to: newline) <= 512,
                      let request = (try? JSONSerialization.jsonObject(with: pending[..<newline])) as? [String: Any]
                else { return }
                pending.removeSubrange(...newline)
                let reply = DispatchQueue.main.sync { self.respond(request) }
                guard InputSocket.send(fd, reply) else { return }
            }
            if pending.count > 512 { return }
        }
    }

    private var permission: Bool { simulate || (AXIsProcessTrusted() && CGPreflightPostEventAccess()) }
    private var foreground: pid_t? {
        if simulate { return simulatedFocus ? 123 : nil }
        let app = NSWorkspace.shared.frontmostApplication
        return app?.bundleIdentifier == gameID ? app?.processIdentifier : nil
    }

    @objc func arm() {
        guard server >= 0, permission else { permissions(); return }
        guard !session, !preparingLaunchLog, launchDeadline == nil, questProcess == nil else { return }
        armed = true
        updateStatus()
    }

    @objc private func runPrepared() {
        let reply = requestPrepared()
        if let message = reply["error"] as? String { state.title = message }
    }

    private func requestPrepared() -> [String: Any] {
        guard !simulate else { return error("模拟执行器不能启动实机任务") }
        guard server >= 0, !session, !preparingLaunchLog, questProcess == nil, launchDeadline == nil else {
            return error("键鼠执行器尚未就绪或已有任务在运行")
        }
        preparingLaunchLog = true
        preparationCancelled = false
        questRequestedAt = Date()
        let deadline = ProcessInfo.processInfo.systemUptime + 120
        state.title = "已请求运行任务，正在准备启动日志"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let log = try self.questRoot().appendingPathComponent("data/logs/quest-launch.log")
                try FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
                try Data("已请求运行任务。\n".utf8).write(to: log)
                DispatchQueue.main.async { self.finishLaunchLog(deadline: deadline, failure: nil) }
            } catch {
                DispatchQueue.main.async {
                    self.finishLaunchLog(deadline: deadline, failure: error.localizedDescription)
                }
            }
        }
        return ["ok": true]
    }

    private func finishLaunchLog(deadline: TimeInterval, failure: String?) {
        preparingLaunchLog = false
        if preparationCancelled {
            if failure == nil { recordLaunchStatus("启动准备已取消") }
            return
        }
        if let failure {
            state.title = "无法写入任务启动日志 · \(failure)"
            return
        }
        let now = ProcessInfo.processInfo.systemUptime
        if now >= deadline {
            state.title = "启动等待超时 · 启动日志"
            recordLaunchStatus(state.title)
            return
        }
        launchDeadline = deadline
        nextLaunchCheck = now
        launchWait = nil
        state.title = "已请求启动主控，正在核对系统权限"
    }

    private func questRoot() throws -> URL {
        guard let resource = Bundle.main.url(forResource: "project-root", withExtension: nil) else {
            throw InputError.message("找不到 WowAgent 项目目录")
        }
        return URL(fileURLWithPath: try String(contentsOf: resource, encoding: .utf8), isDirectory: true)
    }

    private func startPrepared() {
        launchDeadline = nil
        do {
            let root = try questRoot()
            let logURL = root.appendingPathComponent("data/logs/quest-launch.log")
            let log = try FileHandle(forWritingTo: logURL)
            _ = try log.seekToEnd()
            try log.write(contentsOf: Data("权限已通过，启动主控。游戏前台和任务状态将在主控内核对。\n".utf8))
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/bin/zsh")
            task.arguments = [root.appendingPathComponent("macos/quest-accept.command").path, "--run-prepared"]
            task.standardOutput = log
            task.standardError = log
            task.terminationHandler = { [weak self] child in
                try? log.close()
                DispatchQueue.main.async {
                    guard let self, self.questProcess === child else { return }
                    self.questProcess = nil
                    self.stop()
                    self.state.title = child.terminationStatus == 0 ? "任务已完成" : "任务已停止 · 查看运行日志"
                }
            }
            armed = true
            try task.run()
            questProcess = task
            state.title = "正在启动或运行任务"
        } catch {
            stop()
            state.title = "任务启动失败 · \(error)"
            recordLaunchStatus(state.title)
        }
    }

    private func recordLaunchStatus(_ message: String) {
        guard let root = try? questRoot(),
              let log = try? FileHandle(forWritingTo: root.appendingPathComponent("data/logs/quest-launch.log")) else { return }
        defer { try? log.close() }
        do {
            _ = try log.seekToEnd()
            try log.write(contentsOf: Data((message + "\n").utf8))
        } catch { NSLog("WowAgent 任务启动日志写入失败：%@", error.localizedDescription) }
    }

    @objc private func showQuestLog() {
        guard let root = try? questRoot() else { return }
        let result = root.appendingPathComponent("data/logs/quest-session.log")
        let launch = root.appendingPathComponent("data/logs/quest-launch.log")
        let modified = try? result.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate
        let currentResult = modified.map { $0 >= (questRequestedAt ?? .distantPast) } ?? false
        let url = currentResult ? result : launch
        if FileManager.default.fileExists(atPath: url.path) { NSWorkspace.shared.open(url) }
    }

    @objc private func cancel() {
        preparationCancelled = true
        launchDeadline = nil
        let task = questProcess
        questProcess = nil
        if let task, task.isRunning { task.terminate() }
        stop()
    }

    @objc func stop() {
        release()
        armed = false
        session = false
        keepOnline = false
        waitingForForeground = false
        game = nil
        point = nil
        updateStatus()
    }

    @objc private func quit() { cancel(); NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) { cancel() }

    @objc private func permissions() {
        let alert = NSAlert()
        alert.messageText = "WowAgent Keyboard Mouse"
        let accessibility = AXIsProcessTrusted()
        alert.informativeText = "设备控制 \(accessibility ? "已授权" : "未授权")，事件发送 \(CGPreflightPostEventAccess() ? "已授权" : "未授权")。"
        if !accessibility {
            alert.informativeText += "\n请在「隐私与安全性 → 设备控制和数据访问（辅助功能）」中启用当前版。若旧条目已打开，请移除旧条目，再添加并启用当前 App。"
        }
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    @objc private func requestPostPermission() { _ = CGRequestPostEventAccess() }

    private func updateStatus() {
        if keepOnline {
            state.title = waitingForForeground ? "主控在线 · 等待游戏前台与色条" : "主控在线"
        } else {
            state.title = armed ? "已启用，等待或正在执行" : "已停止"
        }
    }

    private func pauseForForeground(_ front: pid_t?) {
        release()
        point = nil
        game = front
        focusEpoch += 1
        waitingForForeground = true
        updateStatus()
    }

    private func watch() {
        let now = ProcessInfo.processInfo.systemUptime
        if let deadline = launchDeadline, now >= nextLaunchCheck {
            nextLaunchCheck = now + 1
            if now >= deadline {
                launchDeadline = nil
                state.title = "启动等待超时 · " + (launchWait?.rawValue ?? "查看运行日志")
                recordLaunchStatus(state.title)
            } else {
                checkPreparedLaunch()
            }
        }
        guard session else { return }
        let front = foreground
        if keepOnline && front != game {
            pauseForForeground(front)
        } else if let game, game != front {
            stop()
            return
        }
        guard permission else { stop(); return }
        if (!keys.isEmpty || !buttons.isEmpty) &&
            ProcessInfo.processInfo.systemUptime - lastContact > 2 { stop() }
    }

    private func checkPreparedLaunch() {
        let waiting: LaunchWait
        if !AXIsProcessTrusted() {
            waiting = .accessibility
        } else if !CGPreflightPostEventAccess() {
            waiting = .postEvents
        } else {
            startPrepared()
            return
        }
        guard waiting != launchWait else { return }
        launchWait = waiting
        state.title = waiting.rawValue
        recordLaunchStatus(waiting.rawValue)
        switch waiting {
        case .accessibility:
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
        case .postEvents:
            _ = CGRequestPostEventAccess()
        }
    }

    private func respond(_ request: [String: Any]) -> [String: Any] {
        guard let op = request["op"] as? String else { return error("缺少键鼠操作") }
        watch()
        if op == "info" {
            return ["ok": true, "simulate": simulate, "armed": armed, "events": eventCount, "navigationPulse": true]
        }
        if op == "run_prepared" { return requestPrepared() }
        if simulate && op == "simulate_blur" {
            simulatedFocus = false
            watch()
            return ["ok": true]
        }
        if simulate && op == "simulate_focus" {
            simulatedFocus = true
            watch()
            return ["ok": true]
        }
        if simulate && op == "simulate_stop" {
            stop()
            return ["ok": true]
        }
        if simulate && op == "simulate_arm" {
            arm()
            return ["ok": true]
        }
        if op == "ready" {
            guard !session, armed, permission else { return error("请给键鼠 App 授权并选择「启用一次会话」") }
            session = true
            keepOnline = request["keep_online"] as? Bool == true && (questProcess != nil || simulate)
            if keepOnline {
                pauseForForeground(foreground)
                return ["ok": true, "focus_epoch": focusEpoch]
            }
            return ["ok": true]
        }
        if op == "release" { release(); return ["ok": true] }
        guard session, armed, permission else { stop(); return error("键鼠会话已停止") }
        lastContact = ProcessInfo.processInfo.systemUptime
        if op == "heartbeat" {
            return waitingForForeground ? ["ok": true, "focus_epoch": focusEpoch] : ["ok": true]
        }
        let front = foreground
        if keepOnline && front != game { pauseForForeground(front) }
        if waitingForForeground {
            if op == "resume_foreground", front != nil, request["focus_epoch"] as? Int == focusEpoch {
                waitingForForeground = false
                updateStatus()
                return ["ok": true]
            }
            return ["ok": false, "error": "等待魔兽前台和新色条", "focus_epoch": focusEpoch]
        }
        guard let pid = front else {
            if op == "prepare_quest", game == nil { return error("等待魔兽回到前台再准备任务") }
            stop()
            return error("魔兽窗口不在前台")
        }
        if op != "prepare_quest" { game = pid }

        switch op {
        case "run_status_v2":
            guard request["wire"] as? Int == 2, let state = request["state"] as? Int,
                  (0...9).contains(state) else { return error("无效的运行状态协议") }
            // Carbon virtual key codes for F13 through F20. F20 is the prefix
            // for states 7–9. No status event uses a normal action-bar key.
            let functionKeys: [CGKeyCode] = [105, 107, 113, 106, 64, 79, 80, 90]
            let symbols = state < 7 ? [state] : [7, state - 7]
            for symbol in symbols {
                let code = functionKeys[symbol]
                guard !keys.contains(code),
                      let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
                      let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
                    return error("无效或冲突的运行状态键")
                }
                down.flags = []
                up.flags = []
                guard send(down, to: pid), send(up, to: pid) else { return error("运行状态发送失败") }
            }
        case "prepare_quest":
            guard keys.isEmpty, buttons.isEmpty else { return error("准备任务前仍有键鼠输入") }
            guard let code = Self.keyCodes["f5"],
                  let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
                  let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
                return error("无法构造任务准备键")
            }
            down.flags = [.maskControl, .maskAlternate, .maskShift]
            up.flags = down.flags
            guard send(down, to: pid), send(up, to: pid) else { return error("任务准备键发送失败") }
        case "down", "up":
            guard let key = request["key"] as? String, let code = Self.keyCodes[key] else {
                return error("按键不在允许列表")
            }
            let down = op == "down"
            if navigationPulse?.key == code {
                guard !down else { return error("该移动键正在短按") }
                navigationPulse = nil
            }
            if down != keys.contains(code) {
                guard send(CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down), to: pid)
                else { return error("无法构造按键事件") }
                if down { keys.insert(code) } else { keys.remove(code) }
            }
        case "navigation_pulse":
            guard let key = request["key"] as? String, Self.pulseKeys.contains(key),
                  let code = Self.keyCodes[key], let microseconds = request["hold_us"] as? Int,
                  (1...120_000).contains(microseconds) else { return error("无效的导航短按") }
            guard navigationPulse == nil, !keys.contains(code) else { return error("已有移动短按或按键被占用") }
            guard send(CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true), to: pid)
                else { return error("无法构造导航短按") }
            let token = UUID()
            navigationPulse = (code, token)
            keys.insert(code)
            DispatchQueue.main.asyncAfter(deadline: .now() + .microseconds(microseconds)) { [weak self] in
                guard let self = self, self.navigationPulse?.token == token else { return }
                self.finishNavigationPulse(pid)
            }
        case "cancel_navigation_pulse":
            finishNavigationPulse(pid)
        case "position":
            guard let p = pointer else { return error("无法读取鼠标位置") }
            return ["ok": true, "x": Int(p.x), "y": Int(p.y)]
        case "move", "mouse_down", "mouse_up", "scroll":
            return mouse(op, request, pid)
        case "world_click":
            return clickVerifiedTarget(pid, right: true)
        case "ui_click":
            return clickVerifiedTarget(pid, right: false)
        default:
            return error("未知键鼠操作")
        }
        return ["ok": true]
    }

    private var pointer: CGPoint? { point ?? (simulate ? CGPoint(x: 200, y: 200) : CGEvent(source: nil)?.location) }

    private func gameContains(_ point: CGPoint, pid: pid_t) -> Bool {
        if simulate { return CGRect(x: -1920, y: 0, width: 3840, height: 1080).contains(point) }
        let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] ?? []
        for window in windows {
            guard (window[kCGWindowLayer as String] as? Int) == 0,
                  let bounds = window[kCGWindowBounds as String] as? [String: Any],
                  let rect = CGRect(dictionaryRepresentation: bounds as CFDictionary), rect.contains(point)
            else { continue }
            return (window[kCGWindowOwnerPID as String] as? Int32) == pid
        }
        return false
    }

    private func mouse(_ op: String, _ request: [String: Any], _ pid: pid_t) -> [String: Any] {
        guard let current = pointer else { return error("无法读取鼠标位置") }
        if op == "move" {
            guard let x = request["x"] as? Int, let y = request["y"] as? Int,
                  (-32768...32768).contains(x), (-32768...32768).contains(y) else { return error("鼠标坐标无效") }
            let next = CGPoint(x: x, y: y)
            guard gameContains(next, pid: pid) else { return error("鼠标目标在魔兽窗口之外") }
            let right = buttons.contains("right")
            let kind: CGEventType = right ? .rightMouseDragged : (buttons.contains("left") ? .leftMouseDragged : .mouseMoved)
            let event = CGEvent(mouseEventSource: nil, mouseType: kind, mouseCursorPosition: next,
                                mouseButton: right ? .right : .left)
            event?.setIntegerValueField(.mouseEventDeltaX, value: Int64(next.x - current.x))
            event?.setIntegerValueField(.mouseEventDeltaY, value: Int64(next.y - current.y))
            // A targeted move carries coordinates but does not set the system pointer
            // that the game reads for world hover. Keep relative camera drags separate.
            if !simulate && buttons.isEmpty && CGWarpMouseCursorPosition(next) != .success {
                return error("无法将系统鼠标移动到魔兽窗口内的目标")
            }
            guard send(event, to: pid) else { return error("无法构造鼠标事件") }
            point = next
        } else if op == "scroll" {
            guard gameContains(current, pid: pid), let delta = request["dy"] as? Int,
                  (-8...8).contains(delta) else { return error("滚轮目标或幅度无效") }
            let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1,
                                wheel1: Int32(delta), wheel2: 0, wheel3: 0)
            event?.location = current
            guard send(event, to: pid) else { return error("无法构造滚轮事件") }
        } else {
            guard let button = request["button"] as? String, ["left", "right"].contains(button)
            else { return error("鼠标按键无效") }
            let down = op == "mouse_down"
            if down != buttons.contains(button) {
                guard !down || gameContains(current, pid: pid) else { return error("鼠标目标在魔兽窗口之外") }
                guard send(buttonEvent(button, down, current), to: pid) else { return error("无法构造点击事件") }
                if down { buttons.insert(button) } else { buttons.remove(button) }
            }
        }
        return ["ok": true]
    }

    private func clickVerifiedTarget(_ pid: pid_t, right: Bool) -> [String: Any] {
        guard keys.isEmpty, buttons.isEmpty, let expected = point,
              let actual = simulate ? point : CGEvent(source: nil)?.location,
              hypot(expected.x - actual.x, expected.y - actual.y) <= 4,
              foreground == pid, gameContains(actual, pid: pid) else {
            return error("点击前鼠标、前台或按键状态已变化，请重新核对目标")
        }
        let source = CGEventSource(stateID: .hidSystemState)
        let button: CGMouseButton = right ? .right : .left
        guard let down = CGEvent(mouseEventSource: source, mouseType: right ? .rightMouseDown : .leftMouseDown,
                                 mouseCursorPosition: actual, mouseButton: button),
              let up = CGEvent(mouseEventSource: source, mouseType: right ? .rightMouseUp : .leftMouseUp,
                               mouseCursorPosition: actual, mouseButton: button) else {
            return error("无法构造点击事件")
        }
        down.flags = []
        up.flags = []
        down.setIntegerValueField(.mouseEventClickState, value: 1)
        up.setIntegerValueField(.mouseEventClickState, value: 1)
        // Both world objects and native UI may poll system button state. Send a
        // complete click through HID only after checking the verified position.
        if !simulate { down.post(tap: .cghidEventTap) }
        eventCount += 1
        defer {
            up.timestamp = DispatchTime.now().uptimeNanoseconds
            if !simulate { up.post(tap: .cghidEventTap) }
            eventCount += 1
        }
        if !simulate { usleep(80000) }
        return ["ok": true]
    }

    private func buttonEvent(_ button: String, _ down: Bool, _ point: CGPoint) -> CGEvent? {
        let right = button == "right"
        let kind: CGEventType = right ? (down ? .rightMouseDown : .rightMouseUp)
                                      : (down ? .leftMouseDown : .leftMouseUp)
        return CGEvent(mouseEventSource: nil, mouseType: kind, mouseCursorPosition: point,
                       mouseButton: right ? .right : .left)
    }

    private func send(_ event: CGEvent?, to pid: pid_t) -> Bool {
        guard let event else { return false }
        if !simulate { event.postToPid(pid) }
        eventCount += 1
        return true
    }

    private func finishNavigationPulse(_ pid: pid_t) {
        guard let pulse = navigationPulse else { return }
        navigationPulse = nil
        if !send(CGEvent(keyboardEventSource: nil, virtualKey: pulse.key, keyDown: false), to: pid) {
            stop()
            return
        }
        keys.remove(pulse.key)
    }

    private func release() {
        navigationPulse = nil
        if let pid = game, simulate || NSRunningApplication(processIdentifier: pid)?.bundleIdentifier == gameID {
            for code in keys { _ = send(CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false), to: pid) }
            if let point = pointer {
                for button in buttons { _ = send(buttonEvent(button, false, point), to: pid) }
            }
        }
        keys.removeAll()
        buttons.removeAll()
    }

    private func error(_ message: String) -> [String: Any] { ["ok": false, "error": message] }
}

@main
enum KeyboardMouseApp {
    static func main() {
        if CommandLine.arguments.dropFirst().elementsEqual(["--permissions"]) {
            print("辅助功能 \(AXIsProcessTrusted())，事件发送 \(CGPreflightPostEventAccess())")
            return
        }
        let smoke = CommandLine.arguments.dropFirst().elementsEqual(["--smoke"])
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = KeyboardMouse(simulate: smoke)
        app.delegate = delegate
        if smoke {
            do {
                try delegate.start()
                delegate.arm()
                print("SIMULATION READY")
                fflush(stdout)
            } catch { fputs("\(error)\n", stderr); exit(2) }
            app.delegate = nil
        }
        withExtendedLifetime(delegate) { app.run() }
    }
}
