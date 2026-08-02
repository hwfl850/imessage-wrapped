import AppKit
import SwiftUI
import WebKit

/// Hosts the report — the same page the command-line version serves, unchanged.
struct ReportView: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()  // leave nothing behind on disk

        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.allowsBackForwardNavigationGestures = false
        web.setValue(false, forKey: "drawsBackground")  // let the window's own colour show
        web.load(URLRequest(url: url))
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {
        guard web.url == nil else { return }
        web.load(URLRequest(url: url))
    }

    final class Coordinator: NSObject, WKNavigationDelegate {

        /// The report's "save" and "export" buttons navigate to endpoints that hand back
        /// a file. A web view has nowhere to put a download, so those two go to the
        /// default browser, which drops them in Downloads like any other file.
        private static let downloadPaths = ["/api/export", "/api/save"]

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.allow)
                return
            }

            if Self.downloadPaths.contains(where: { url.path.hasPrefix($0) }) {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }

            // Anything that isn't our own loopback server belongs in a real browser.
            if url.host != "127.0.0.1" && url.scheme != "about" {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }

            decisionHandler(.allow)
        }
    }
}
