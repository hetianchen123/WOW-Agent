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
    private var game: pid_t?
    private var lastContact = ProcessInfo.processInfo.systemUptime
    private var keys = Set<CGKeyCode>()
    private var buttons = Set<String>()
    private var point: CGPoint?
    private var eventCount = 0
    private var simulatedFocus = true
    private var timer: Timer?

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

    func applicationDidFinishLaunching(_ notification: Notification) {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = simulate ? "WoW 输入模拟" : "WoW 键鼠"
        let menu = NSMenu()
        menu.addItem(state)
        for (title, action) in [("启用一次会话", #selector(arm)), ("立即停止", #selector(stop)),
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
        guard !session else { return }
        armed = true
        updateStatus()
    }

    @objc func stop() {
        release()
        armed = false
        session = false
        game = nil
        point = nil
        updateStatus()
    }

    @objc private func quit() { stop(); NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) { stop() }

    @objc private func permissions() {
        let alert = NSAlert()
        alert.messageText = "WowAgent Keyboard Mouse"
        alert.informativeText = "辅助功能 \(AXIsProcessTrusted() ? "已授权" : "未授权")，事件发送 \(CGPreflightPostEventAccess() ? "已授权" : "未授权")。\n请在「隐私与安全性 → 辅助功能」中启用本 App。"
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    @objc private func requestPostPermission() { _ = CGRequestPostEventAccess() }

    private func updateStatus() { state.title = armed ? "已启用，等待或正在执行" : "已停止" }

    private func watch() {
        guard session else { return }
        if let game, game != foreground { stop(); return }
        if (!keys.isEmpty || !buttons.isEmpty) &&
            ProcessInfo.processInfo.systemUptime - lastContact > 2 { stop() }
    }

    private func respond(_ request: [String: Any]) -> [String: Any] {
        guard let op = request["op"] as? String else { return error("缺少键鼠操作") }
        watch()
        if op == "info" { return ["ok": true, "simulate": simulate, "armed": armed, "events": eventCount] }
        if simulate && op == "simulate_blur" {
            simulatedFocus = false
            watch()
            return ["ok": true]
        }
        if op == "ready" {
            guard armed, permission else { return error("请给键鼠 App 授权并选择「启用一次会话」") }
            session = true
            return ["ok": true]
        }
        if op == "release" { release(); return ["ok": true] }
        guard session, armed, permission else { stop(); return error("键鼠会话已停止") }
        lastContact = ProcessInfo.processInfo.systemUptime
        if op == "heartbeat" { return ["ok": true] }
        guard let pid = foreground else { stop(); return error("魔兽窗口不在前台") }
        game = pid

        switch op {
        case "down", "up":
            guard let key = request["key"] as? String, let code = Self.keyCodes[key] else {
                return error("按键不在允许列表")
            }
            let down = op == "down"
            if down != keys.contains(code) {
                guard send(CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down), to: pid)
                else { return error("无法构造按键事件") }
                if down { keys.insert(code) } else { keys.remove(code) }
            }
        case "position":
            guard let p = pointer else { return error("无法读取鼠标位置") }
            return ["ok": true, "x": Int(p.x), "y": Int(p.y)]
        case "move", "mouse_down", "mouse_up", "scroll":
            return mouse(op, request, pid)
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

    private func release() {
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
