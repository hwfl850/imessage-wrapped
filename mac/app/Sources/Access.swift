import AppKit
import Foundation

/// Everything to do with Full Disk Access.
///
/// macOS gives no API to request Full Disk Access and no API to ask whether you hold
/// it. The only honest test is to read a protected file and see what the kernel says,
/// so that is exactly what `state()` does — 16 bytes, no copy, no SQLite.
enum Access {

    enum State: Equatable {
        /// chat.db opened and read. We're good.
        case granted
        /// chat.db exists but the read was refused — Full Disk Access is off.
        case denied
        /// No Messages database on this Mac at all. Permission won't help.
        case noMessagesDatabase
    }

    static var chatDatabase: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Messages/chat.db")
    }

    static func state() -> State {
        let url = chatDatabase

        // Distinguish "no Messages on this Mac" from "not allowed to look". Without
        // Full Disk Access we cannot stat the file either, so a missing-file answer
        // here is only trustworthy once the directory itself is reachable.
        guard let handle = try? FileHandle(forReadingFrom: url) else {
            let library = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Messages")
            if FileManager.default.isReadableFile(atPath: library.path),
               !FileManager.default.fileExists(atPath: url.path) {
                return .noMessagesDatabase
            }
            return .denied
        }
        defer { try? handle.close() }

        // Opening can succeed where reading still fails, so make it read real bytes.
        guard (try? handle.read(upToCount: 16)) != nil else { return .denied }
        return .granted
    }

    /// Opens System Settings directly on Privacy & Security → Full Disk Access.
    ///
    /// Because we already tried to read chat.db on launch, macOS has added this app to
    /// that list with its switch off — the user only has to flip it, never to go
    /// hunting through Applications with the "+" button.
    static func openPrivacySettings() {
        let deepLink = URL(
            string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!
        if !NSWorkspace.shared.open(deepLink) {
            // Older or reorganised Settings builds: land them in the app at least.
            NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/System Settings.app"))
        }
    }

    /// Quits and reopens the app.
    ///
    /// A process that was already running when the switch was flipped usually keeps its
    /// old, cached refusal, so granting access genuinely does require a restart. The
    /// detached shell waits for this instance to exit before reopening the bundle.
    static func relaunch() {
        let bundlePath = Bundle.main.bundlePath
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/sh")
        task.arguments = [
            "-c",
            "sleep 0.6; /usr/bin/open -n \"$1\"", "--", bundlePath,
        ]
        try? task.run()
        NSApp.terminate(nil)
    }
}
