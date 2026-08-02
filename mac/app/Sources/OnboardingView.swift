import AppKit
import SwiftUI

// MARK: - Shared furniture

/// The app's own icon, straight from the bundle, so the window and the Dock agree.
private struct AppIcon: View {
    var size: CGFloat = 72

    var body: some View {
        Image(nsImage: NSApp.applicationIconImage)
            .resizable()
            .frame(width: size, height: size)
    }
}

private struct Screen<Content: View>: View {
    let content: Content
    init(@ViewBuilder content: () -> Content) { self.content = content() }

    var body: some View {
        VStack(spacing: 0) { content }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .padding(.horizontal, 44)
            .padding(.vertical, 36)
    }
}

private struct Title: View {
    let text: String
    let detail: String

    var body: some View {
        VStack(spacing: 9) {
            Text(text)
                .font(.system(size: 25, weight: .semibold, design: .rounded))
                .multilineTextAlignment(.center)
            Text(detail)
                .font(.system(size: 13.5))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineSpacing(2.5)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - 1. Welcome

struct WelcomeView: View {
    let onContinue: () -> Void

    private struct Promise: View {
        let symbol: String
        let headline: String
        let detail: String

        var body: some View {
            HStack(alignment: .top, spacing: 13) {
                Image(systemName: symbol)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(Color.accentColor)
                    .frame(width: 22, height: 20)
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline).font(.system(size: 13, weight: .medium))
                    Text(detail)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
        }
    }

    var body: some View {
        Screen {
            Spacer(minLength: 0)

            AppIcon()
                .padding(.bottom, 20)

            Title(
                text: "iMessage Wrapped",
                detail: "Your Messages history, turned into a year in review.")

            VStack(alignment: .leading, spacing: 17) {
                Promise(
                    symbol: "lock.shield",
                    headline: "Nothing leaves this Mac",
                    detail: "No account, no upload, no network connection of any kind.")
                Promise(
                    symbol: "eye.slash",
                    headline: "Read-only",
                    detail: "Your messages are counted, never changed or deleted.")
                Promise(
                    symbol: "arrow.uturn.backward",
                    headline: "Yours to undo",
                    detail: "Turn the permission back off the moment you're finished.")
            }
            .padding(.top, 30)
            .padding(.horizontal, 6)

            Spacer(minLength: 24)
            Spacer(minLength: 0)

            Button(action: onContinue) {
                Text("Continue").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .keyboardShortcut(.defaultAction)
        }
    }
}

// MARK: - 2. Full Disk Access

struct PermissionView: View {
    let granted: Bool
    let offerRestart: Bool
    let onOpenSettings: () -> Void
    let onRestart: () -> Void

    private struct Step: View {
        let number: Int
        let text: AttributedString

        var body: some View {
            HStack(alignment: .firstTextBaseline, spacing: 11) {
                Text("\(number)")
                    .font(.system(size: 11, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white)
                    .frame(width: 19, height: 19)
                    .background(Circle().fill(Color.accentColor))
                Text(text)
                    .font(.system(size: 13))
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
        }
    }

    /// Bolds the parts the user has to actually find on screen.
    private func step(_ plain: String, emphasise: String) -> AttributedString {
        var text = AttributedString(plain)
        if let range = text.range(of: emphasise) {
            text[range].font = .system(size: 13, weight: .semibold)
        }
        return text
    }

    var body: some View {
        Screen {
            Image(systemName: granted ? "checkmark.shield.fill" : "lock.shield.fill")
                .font(.system(size: 41, weight: .regular))
                .foregroundStyle(granted ? Color.green : Color.accentColor)
                .frame(height: 52)
                .padding(.bottom, 18)

            Title(
                text: granted ? "You're all set" : "One permission to go",
                detail: granted
                    ? "Opening your Wrapped…"
                    : "macOS keeps your Messages library private. It needs your say-so "
                        + "before this app can read it.")

            if !granted {
                VStack(alignment: .leading, spacing: 15) {
                    Step(number: 1, text: step(
                        "Click Open Privacy Settings below.",
                        emphasise: "Open Privacy Settings"))
                    Step(number: 2, text: step(
                        "Find iMessage Wrapped in the list — it's already there.",
                        emphasise: "iMessage Wrapped"))
                    Step(number: 3, text: step(
                        "Switch it on. This window continues by itself.",
                        emphasise: "Switch it on."))
                }
                .padding(18)
                .background(
                    RoundedRectangle(cornerRadius: 11, style: .continuous)
                        .fill(Color(nsColor: .controlBackgroundColor)))
                .overlay(
                    RoundedRectangle(cornerRadius: 11, style: .continuous)
                        .strokeBorder(Color.primary.opacity(0.08)))
                .padding(.top, 26)
            }

            Spacer(minLength: 20)

            StatusPill(granted: granted)
                .padding(.bottom, granted ? 0 : 16)

            if !granted {
                Button(action: onOpenSettings) {
                    Text("Open Privacy Settings").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)

                // Only surfaced once waiting has clearly stalled — macOS sometimes holds
                // on to its earlier refusal until the app is launched again.
                if offerRestart {
                    Button("Switched it on but nothing happened? Restart", action: onRestart)
                        .buttonStyle(.link)
                        .font(.system(size: 12))
                        .padding(.top, 11)
                        .transition(.opacity)
                }
            }
        }
        .animation(.easeInOut(duration: 0.28), value: granted)
        .animation(.easeInOut(duration: 0.28), value: offerRestart)
    }
}

private struct StatusPill: View {
    let granted: Bool

    var body: some View {
        HStack(spacing: 7) {
            if granted {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(Color.green)
            } else {
                ProgressView()
                    .controlSize(.small)
                    .scaleEffect(0.75)
                    .frame(width: 13, height: 13)
            }
            Text(granted ? "Permission granted" : "Waiting for permission…")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(granted ? Color.green : Color.secondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(
            Capsule().fill(
                (granted ? Color.green : Color.secondary).opacity(0.11)))
    }
}

// MARK: - 3. Waiting / problems

struct StartingView: View {
    var body: some View {
        Screen {
            Spacer()
            ProgressView().controlSize(.large)
            Text("Starting up…")
                .font(.system(size: 13))
                .foregroundStyle(.secondary)
                .padding(.top, 17)
            Spacer()
        }
    }
}

struct MessageView: View {
    let symbol: String
    let tint: Color
    let title: String
    let detail: String
    var actionLabel: String?
    var action: (() -> Void)?

    var body: some View {
        Screen {
            Spacer(minLength: 0)
            Image(systemName: symbol)
                .font(.system(size: 39))
                .foregroundStyle(tint)
                .padding(.bottom, 19)
            Title(text: title, detail: detail)
            Spacer(minLength: 24)
            if let actionLabel, let action {
                Button(action: action) {
                    Text(actionLabel).frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
            }
        }
    }
}
