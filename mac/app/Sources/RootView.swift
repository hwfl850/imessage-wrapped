import SwiftUI

struct RootView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Group {
            switch model.phase {
            case .welcome:
                WelcomeView(onContinue: model.continueFromWelcome)

            case .permission:
                PermissionView(
                    granted: model.accessGranted,
                    offerRestart: model.waitingLongEnoughToOfferRestart,
                    onOpenSettings: Access.openPrivacySettings,
                    onRestart: Access.relaunch)

            case .noMessagesDatabase:
                MessageView(
                    symbol: "message.badge.filled.fill",
                    tint: .secondary,
                    title: "No Messages history here",
                    detail: "This Mac has no Messages library to read. Sign in to Messages, "
                        + "let it sync, then open iMessage Wrapped again.",
                    actionLabel: "Check Again",
                    action: model.retry)

            case .starting:
                StartingView()

            case .ready(let url):
                ReportView(url: url)

            case .failed(let why):
                MessageView(
                    symbol: "exclamationmark.triangle.fill",
                    tint: .orange,
                    title: "Something went wrong",
                    detail: why,
                    actionLabel: "Try Again",
                    action: model.retry)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
