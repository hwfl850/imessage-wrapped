import AppKit
import Combine
import Foundation

/// Drives the whole app: which screen is up, and when to start the engine.
final class AppModel: ObservableObject {

    enum Phase: Equatable {
        /// First run — explain what this is before asking for anything.
        case welcome
        /// Walking the user through the Full Disk Access switch.
        case permission
        /// Messages has never been set up here; permission wouldn't help.
        case noMessagesDatabase
        /// Engine is booting.
        case starting
        /// The report is live at this address.
        case ready(URL)
        case failed(String)
    }

    @Published private(set) var phase: Phase = .starting
    @Published private(set) var accessGranted = false
    /// Set once the user has been waiting a while with nothing happening, so the
    /// restart escape hatch can appear without cluttering the first few seconds.
    @Published private(set) var waitingLongEnoughToOfferRestart = false

    private let engine = Engine()
    private var poll: Timer?
    private var waitStarted: Date?

    /// True when the app has been launched before and already cleared onboarding, so
    /// returning users go straight to their report.
    private var hasOnboarded: Bool {
        get { UserDefaults.standard.bool(forKey: "hasCompletedOnboarding") }
        set { UserDefaults.standard.set(newValue, forKey: "hasCompletedOnboarding") }
    }

    func start() {
        switch Access.state() {
        case .granted:
            accessGranted = true
            hasOnboarded = true
            launchEngine()
        case .noMessagesDatabase:
            phase = .noMessagesDatabase
        case .denied:
            // Someone who has done this before and later revoked access doesn't need
            // the welcome pitch again — send them straight to the switch.
            phase = hasOnboarded ? .permission : .welcome
            if phase == .permission { beginPolling() }
        }
    }

    func continueFromWelcome() {
        phase = .permission
        beginPolling()
    }

    // MARK: - Waiting for the switch

    /// Re-checks access once a second.
    ///
    /// Testing from inside this process is exactly the right test: if the read succeeds
    /// here, this process can do the real work, and no relaunch is needed. If macOS is
    /// holding a cached refusal it simply never succeeds, and the restart button — which
    /// appears shortly — is the way out.
    private func beginPolling() {
        guard poll == nil else { return }
        waitStarted = Date()
        let timer = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
        RunLoop.main.add(timer, forMode: .common)
        poll = timer
    }

    private func tick() {
        if let started = waitStarted, Date().timeIntervalSince(started) > 8,
           !waitingLongEnoughToOfferRestart {
            waitingLongEnoughToOfferRestart = true
        }
        guard Access.state() == .granted else { return }

        stopPolling()
        accessGranted = true
        hasOnboarded = true

        // Let the "granted" state land visibly before moving on; jumping instantly
        // reads as a glitch rather than as confirmation.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [weak self] in
            self?.launchEngine()
        }
    }

    private func stopPolling() {
        poll?.invalidate()
        poll = nil
    }

    // MARK: - Engine

    private func launchEngine() {
        phase = .starting
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let url = try self.engine.start()
                DispatchQueue.main.async { self.phase = .ready(url) }
            } catch {
                DispatchQueue.main.async {
                    self.phase = .failed(error.localizedDescription)
                }
            }
        }
    }

    func retry() {
        waitingLongEnoughToOfferRestart = false
        start()
    }

    func shutDown() {
        stopPolling()
        engine.stop()
    }
}
