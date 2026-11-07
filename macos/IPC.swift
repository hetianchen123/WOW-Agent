import Foundation
import Darwin
import Security

enum InputError: Error, CustomStringConvertible {
    case message(String)
    var description: String {
        switch self { case .message(let text): return text }
    }
}

enum InputSocket {
    static func path(simulate: Bool) throws -> String {
        let directory = "/private/tmp/wowagent-input-\(getuid())"
        if mkdir(directory, 0o700) != 0 && errno != EEXIST {
            throw InputError.message("无法创建键鼠连接目录")
        }
        var info = stat()
        guard lstat(directory, &info) == 0, info.st_uid == getuid(),
              info.st_mode & S_IFMT == S_IFDIR, info.st_mode & 0o077 == 0 else {
            throw InputError.message("键鼠连接目录的所有者或权限不正确")
        }
        return directory + (simulate ? "/simulate.sock" : "/input.sock")
    }

    static func withAddress<T>(_ path: String, _ body: (UnsafePointer<sockaddr>, socklen_t) -> T) throws -> T {
        var address = sockaddr_un()
        let bytes = Array(path.utf8CString)
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            throw InputError.message("键鼠连接路径太长")
        }
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes.map(UInt8.init(bitPattern:))) }
        return withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                body($0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
    }

    static func configure(_ fd: Int32) {
        var enabled: Int32 = 1
        var timeout = timeval(tv_sec: 1, tv_usec: 0)
        _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &enabled, socklen_t(MemoryLayout<Int32>.size))
        _ = setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
    }

    static func connect(simulate: Bool) throws -> FileHandle {
        let path = try path(simulate: simulate)
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw InputError.message("无法创建键鼠连接") }
        configure(fd)
        guard try withAddress(path, { Darwin.connect(fd, $0, $1) }) == 0 else {
            close(fd)
            throw InputError.message("请先打开 WowAgent Keyboard Mouse.app")
        }
        return FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    }

    static func peerPID(_ fd: Int32) -> pid_t? {
        var uid: uid_t = 0
        var gid: gid_t = 0
        var pid: pid_t = 0
        var size = socklen_t(MemoryLayout<pid_t>.size)
        guard getpeereid(fd, &uid, &gid) == 0, uid == getuid(),
              getsockopt(fd, SOL_LOCAL, LOCAL_PEERPID, &pid, &size) == 0, pid > 0 else { return nil }
        return pid
    }

    static func validateController(_ fd: Int32, requirement: SecRequirement) -> Bool {
        guard let pid = peerPID(fd) else { return false }
        var code: SecCode?
        let attributes = [kSecGuestAttributePid as String: pid] as CFDictionary
        guard SecCodeCopyGuestWithAttributes(nil, attributes, SecCSFlags(), &code) == errSecSuccess,
              let code else { return false }
        return SecCodeCheckValidity(code, SecCSFlags(), requirement) == errSecSuccess
    }

    static func send(_ fd: Int32, _ response: [String: Any]) -> Bool {
        guard var data = try? JSONSerialization.data(withJSONObject: response) else { return false }
        data.append(10)
        return data.withUnsafeBytes { raw in
            guard let start = raw.baseAddress else { return false }
            var offset = 0
            while offset < raw.count {
                let n = Darwin.send(fd, start + offset, raw.count - offset, 0)
                if n < 0 && errno == EINTR { continue }
                if n <= 0 { return false }
                offset += n
            }
            return true
        }
    }
}
