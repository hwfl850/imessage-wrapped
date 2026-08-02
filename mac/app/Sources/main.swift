import AppKit

// Built without an Xcode project, so the application is wired up by hand rather than
// through @main. Top-level code belongs in a file called main.swift.
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
