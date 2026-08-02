import Foundation

/// Supervises the bundled Python analysis server.
///
/// The engine is the original command-line program, unchanged apart from a startup
/// banner that tells us which port it landed on. It binds loopback only, so nothing on
/// the network can reach it.
final class Engine {

    enum StartupError: LocalizedError {
        case missingExecutable(String)
        case launchFailed(String)
        case noHandshake(String)

        var errorDescription: String? {
            switch self {
            case .missingExecutable(let path):
                return "The analysis engine is missing from this app bundle (\(path))."
            case .launchFailed(let why):
                return "The analysis engine could not start: \(why)"
            case .noHandshake(let log):
                return log.isEmpty
                    ? "The analysis engine started but never reported a port."
                    : "The analysis engine stopped during startup:\n\n\(log)"
            }
        }
    }

    private var process: Process?
    private(set) var url: URL?

    private static let banner = "WRAPPED_PORT"

    private static var executable: URL? {
        Bundle.main.resourceURL?
            .appendingPathComponent("engine/wrapped-engine")
    }

    /// Launches the engine and blocks until it reports its port. Call off the main
    /// thread — startup is normally well under a second but is still real work.
    func start() throws -> URL {
        guard let executable = Self.executable,
              FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw StartupError.missingExecutable(Self.executable?.lastPathComponent ?? "engine")
        }

        let task = Process()
        task.executableURL = executable
        task.arguments = ["--announce-port", "--no-browser"]

        let out = Pipe()
        let err = Pipe()
        task.standardOutput = out
        task.standardError = err

        do {
            try task.run()
        } catch {
            throw StartupError.launchFailed(error.localizedDescription)
        }
        process = task

        guard let port = Self.readPort(from: out.fileHandleForReading) else {
            let log = String(
                data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            task.terminate()
            throw StartupError.noHandshake(log.trimmingCharacters(in: .whitespacesAndNewlines))
        }

        let resolved = URL(string: "http://127.0.0.1:\(port)")!
        url = resolved
        return resolved
    }

    /// Reads stdout a line at a time until the banner shows up or the pipe closes.
    private static func readPort(from handle: FileHandle) -> Int? {
        var buffer = Data()
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { return nil }  // engine exited without announcing
            buffer.append(chunk)

            while let newline = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                let line = String(data: buffer[..<newline], encoding: .utf8) ?? ""
                buffer.removeSubrange(...newline)

                let parts = line.split(separator: " ")
                if parts.count == 2, parts[0] == banner, let port = Int(parts[1]) {
                    return port
                }
            }
        }
    }

    func stop() {
        process?.terminate()
        process = nil
        url = nil
    }
}
