import AppKit
import Combine
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {

    private let model = AppModel()
    private var window: NSWindow!
    private var watch: AnyCancellable?
    private var grownForReport = false

    /// Onboarding is a small fixed panel; the report wants room to breathe.
    private static let onboardingSize = NSSize(width: 520, height: 610)
    private static let reportSize = NSSize(width: 1080, height: 820)

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(
            contentRect: NSRect(origin: .zero, size: Self.onboardingSize),
            styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView],
            backing: .buffered,
            defer: false)
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isMovableByWindowBackground = true

        // NSHostingView publishes an intrinsic size, and as a window's contentView it
        // drives the window from it — a greedy SwiftUI layout was producing a window
        // 3250pt tall with the button far below the screen. A plain container in
        // between severs that: the box decides the size, SwiftUI fills the box.
        let container = NSView(frame: NSRect(origin: .zero, size: Self.onboardingSize))
        let host = NSHostingView(rootView: RootView(model: model))
        host.frame = container.bounds
        host.autoresizingMask = [.width, .height]
        container.addSubview(host)
        window.contentView = container
        window.setContentSize(Self.onboardingSize)
        window.center()
        window.makeKeyAndOrderFront(nil)

        buildMenu()

        watch = model.$phase.receive(on: RunLoop.main).sink { [weak self] phase in
            self?.adjustWindow(for: phase)
        }

        NSApp.activate(ignoringOtherApps: true)
        model.start()
    }

    /// Grows the window once, when the report first appears, and hands back the
    /// resizing and full-screen controls that onboarding has no use for.
    private func adjustWindow(for phase: AppModel.Phase) {
        guard case .ready = phase, !grownForReport else { return }
        grownForReport = true

        window.styleMask.insert(.resizable)
        window.collectionBehavior.insert(.fullScreenPrimary)
        window.title = "iMessage Wrapped"

        window.contentMinSize = NSSize(width: 660, height: 540)

        // Grow around the existing centre so the window doesn't jump across the screen.
        let target = window.frameRect(forContentRect:
            NSRect(origin: .zero, size: Self.reportSize)).size
        var frame = window.frame
        frame.origin.x -= (target.width - frame.width) / 2
        frame.origin.y -= (target.height - frame.height) / 2
        frame.size = target

        if let visible = window.screen?.visibleFrame, !visible.contains(frame) {
            frame.origin.x = visible.midX - target.width / 2
            frame.origin.y = visible.midY - target.height / 2
        }
        window.setFrame(frame, display: true, animate: true)
    }

    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "About iMessage Wrapped", action: #selector(showAbout), keyEquivalent: "")
            .target = self
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Privacy Settings…", action: #selector(openPrivacy), keyEquivalent: "")
            .target = self
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Hide iMessage Wrapped", action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h")
        appMenu.addItem(
            withTitle: "Quit iMessage Wrapped", action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(
            withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        NSApp.mainMenu = main
    }

    @objc private func openPrivacy() { Access.openPrivacySettings() }

    @objc private func showAbout() {
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "iMessage Wrapped",
            .init(rawValue: "Copyright"): "Runs entirely on this Mac. Nothing is uploaded.",
        ])
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        model.shutDown()
    }
}
