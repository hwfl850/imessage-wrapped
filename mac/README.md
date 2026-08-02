# Building the Mac app

```bash
./mac/build.sh
```

Produces `mac/dist/iMessage Wrapped.app` and `mac/dist/iMessage-Wrapped-<version>.dmg`.
Both are universal (arm64 + x86_64). Takes about a minute.

## What it needs

- **Xcode Command Line Tools** — `xcode-select --install`. That's `swiftc`, `lipo`,
  `codesign`, `iconutil` and `hdiutil`; the full Xcode app is not required.
- **A universal Python** — the [python.org](https://www.python.org/downloads/macos/)
  builds are, and Homebrew's are not. `--target-arch universal2` fails on a
  single-architecture interpreter.
- **PyInstaller** — `pip3 install pyinstaller`.

Override the interpreter with `PYTHON=/path/to/python3 ./mac/build.sh` if the one on
your `PATH` isn't the universal one.

## What it does

1. **Icon.** `tools/IconGen.swift` draws it with CoreGraphics at all ten sizes;
   `iconutil` packs the `.iconset` into an `.icns`. No image assets in the repo.
2. **Engine.** PyInstaller freezes `engine/wrapped_engine.py` `--onedir` with its own
   interpreter. The engine has no third-party imports, so a universal build needs
   nothing beyond a universal Python.
3. **App.** `swiftc` compiles `app/Sources/*.swift` once per architecture; `lipo` joins
   them.
4. **Bundle.** Binary, icon and engine are assembled into the `.app`, and
   `app/Resources/Info.plist.template` is filled in.
5. **Signing.** Inside out — every nested Mach-O first, then the bundle around them.
   Signing the outside first would be invalidated the moment the inside changed.
6. **DMG.** A staging folder with the app and an `/Applications` symlink, compressed
   with `hdiutil`.

## Signing for other people

The default is an ad-hoc signature (`codesign -s -`), which is enough to run on the
machine that built it. On anyone else's Mac, Gatekeeper refuses it until they go to
System Settings → Privacy & Security and click **Open Anyway**.

The published releases are ad-hoc signed, so that first-launch step is expected — the
main README walks users through it. A build you compile yourself is signed by your own
machine and opens without any of it.

To ship something that opens cleanly for everyone you need a **Developer ID
Application** certificate, which means a paid Apple Developer account. With one:

```bash
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" ./mac/build.sh

xcrun notarytool submit mac/dist/iMessage-Wrapped-1.0.0.dmg \
    --apple-id you@example.com --team-id TEAMID --password APP_SPECIFIC_PASSWORD --wait
xcrun stapler staple mac/dist/iMessage-Wrapped-1.0.0.dmg
```

`SIGN_IDENTITY` also turns on the hardened runtime and a secure timestamp, both of which
notarisation requires.

Note that a **Developer ID** certificate is the one that matters here. An *Apple
Development* certificate — the kind you get for free — will sign, but won't notarise and
won't satisfy Gatekeeper on another Mac.

This app cannot go on the Mac App Store: App Store apps must be sandboxed, and a
sandboxed app can't hold the Full Disk Access entitlement it needs to read `chat.db`.

## Layout

```
mac/
  app/
    Sources/
      main.swift            NSApplication bootstrap
      AppDelegate.swift     window, menu, growing the window for the report
      AppModel.swift        the phase machine, and polling for the permission
      RootView.swift        phase -> view
      OnboardingView.swift  welcome and Full Disk Access screens
      ReportView.swift      WKWebView wrapper
      Access.swift          the Full Disk Access probe, deep link and relaunch
      Engine.swift          launches the Python engine, reads its port
    Resources/
      Info.plist.template
  engine/
    wrapped_engine.py       the root script, plus a port handshake and a watchdog
  tools/
    IconGen.swift           build-time icon renderer
  build.sh
```

`engine/wrapped_engine.py` is a copy of the `imessage_wrapped.py` at the root of the
repo. The root file remains the standalone version and still runs on its own; the copy
adds three things and changes nothing else:

- `--announce-port`, which prints `WRAPPED_PORT <n>` on stdout instead of the human
  banner, so the app can find the server
- `--probe-access`, a cheap exit-code-only Full Disk Access check
- a watchdog thread that shuts the server down if the app that launched it goes away
