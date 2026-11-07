import AppKit
import Foundation

private let permissionCheck = """
    from Quartz import CGPreflightScreenCaptureAccess
    from ApplicationServices import AXIsProcessTrusted
    print('录屏权限:', bool(CGPreflightScreenCaptureAccess()))
    print('主程序的辅助功能权限:', bool(AXIsProcessTrusted()))
    """

private final class Controller: NSObject, NSApplicationDelegate {
    private let args: [String]
    private let command = NSTextField(string: "calibrate-channel")
    private let log = NSTextView()
    private var window: NSWindow?
    private var process: Process?
    private var output: Pipe?

    init(_ args: [String]) { self.args = args }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 450),
                           styleMask: [.titled, .closable, .miniaturizable, .resizable],
                           backing: .buffered, defer: false)
        win.title = "WowAgent Controller"
        win.center()
        let note = NSTextField(wrappingLabelWithString: "输入 WowAgent 命令参数。实机执行请先在「WoW 键鼠」菜单启用一次会话，启动后切回魔兽。")
        let buttons = NSStackView(views: [
            NSButton(title: "运行", target: self, action: #selector(run)),
            NSButton(title: "停止", target: self, action: #selector(stop)),
            NSButton(title: "检查录屏权限", target: self, action: #selector(permissions)),
        ])
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        log.isEditable = false
        log.isVerticallyResizable = true
        log.textContainer?.widthTracksTextView = true
        log.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        log.autoresizingMask = [.width]
        scroll.documentView = log
        let layout = NSStackView(views: [note, command, buttons, scroll])
        layout.orientation = .vertical
        layout.alignment = .leading
        layout.spacing = 10
        layout.translatesAutoresizingMaskIntoConstraints = false
        let content = win.contentView!
        content.addSubview(layout)
        NSLayoutConstraint.activate([
            layout.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 14),
            layout.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -14),
            layout.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),
            layout.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -14),
            note.widthAnchor.constraint(equalTo: layout.widthAnchor),
            command.widthAnchor.constraint(equalTo: layout.widthAnchor),
            scroll.widthAnchor.constraint(equalTo: layout.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 220),
        ])
        window = win
        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if !args.isEmpty {
            var commandArgs = args
            commandArgs.insert(contentsOf: ["-m", "bot.cli"], at: 0)
            launch(args: commandArgs)
        }
    }

    @objc private func run() {
        launch(args: ["-c", "import shlex, sys; from bot.cli import main; raise SystemExit(main(shlex.split(sys.argv[1])))", command.stringValue])
    }

    @objc private func permissions() {
        launch(args: ["-c", permissionCheck], connectInput: false)
    }

    @objc private func stop() {
        if let process, process.isRunning { process.terminate() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { stop() }

    private func append(_ text: String) {
        log.textStorage?.append(NSAttributedString(string: text, attributes: [.font: log.font!]))
        log.scrollRangeToVisible(NSRange(location: log.string.utf16.count, length: 0))
    }

    private func launch(args: [String], connectInput: Bool = true) {
        guard process == nil else { append("\n已有命令在运行。\n"); return }
        do {
            let task = try makeTask(args: args, simulate: false, connectInput: connectInput)
            let pipe = Pipe()
            task.standardOutput = pipe
            task.standardError = pipe
            log.string = ""
            pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil; return }
                let text = String(decoding: data, as: UTF8.self)
                DispatchQueue.main.async { self?.append(text) }
            }
            task.terminationHandler = { [weak self] child in
                DispatchQueue.main.async {
                    self?.append("\n退出状态 \(child.terminationStatus)\n")
                    self?.process = nil
                    self?.output = nil
                }
            }
            try start(task)
            process = task
            output = pipe
        } catch { append("\n\(error)\n") }
    }
}

private func makeTask(args: [String], simulate: Bool, connectInput: Bool) throws -> Process {
    guard let file = Bundle.main.url(forResource: "project-root", withExtension: nil)
    else { throw InputError.message("找不到 WowAgent 项目位置") }
    let root = URL(fileURLWithPath: try String(contentsOf: file, encoding: .utf8), isDirectory: true)
    let python = root.appendingPathComponent(".venv/bin/python")
    guard FileManager.default.isExecutableFile(atPath: python.path)
    else { throw InputError.message("请在项目根目录安装 .venv 后重新运行") }
    let task = Process()
    task.executableURL = python
    task.currentDirectoryURL = root
    task.arguments = args
    var env = ProcessInfo.processInfo.environment.filter { key, _ in
        !key.hasPrefix("WOWAGENT_") && !key.hasPrefix("PYTHON") && !key.hasPrefix("DYLD_")
    }
    env["WOWAGENT_APP_MODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if connectInput, let input = try? InputSocket.connect(simulate: simulate) {
        task.standardInput = input
        env["WOWAGENT_INPUT_SOCKET"] = "1"
    } else {
        task.standardInput = FileHandle.nullDevice
    }
    task.environment = env
    return task
}

private func start(_ task: Process) throws {
    let input = task.standardInput as? FileHandle
    defer { if input !== FileHandle.nullDevice { try? input?.close() } }
    try task.run()
}

@main
enum ControllerApp {
    static func main() {
        let args = Array(CommandLine.arguments.dropFirst())
        if args == ["--smoke"] || args == ["--permissions"] {
            do {
                let smoke = args == ["--smoke"]
                let pythonArgs = smoke ? ["-m", "bot.control.input_helper"] : ["-c", permissionCheck]
                let task = try makeTask(args: pythonArgs, simulate: smoke, connectInput: smoke)
                try start(task)
                task.waitUntilExit()
                exit(task.terminationStatus)
            } catch { fputs("\(error)\n", stderr); exit(2) }
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = Controller(args)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
