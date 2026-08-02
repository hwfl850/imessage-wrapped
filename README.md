# iMessage Wrapped

Your Messages history, turned into a Spotify-Wrapped-style report — built entirely on
your own Mac.

No installs, no accounts, no network. Open it, click a button, scroll.

### Get the app

**[Download iMessage Wrapped for macOS](../../releases/latest)** — open the `.dmg`, drag
the app to Applications, and launch it. It walks you through the one permission macOS
requires and then shows your report. Universal, so it runs natively on Apple silicon and
Intel.

### Or run the script

Everything the app does, the original single Python file still does on its own:

```bash
python3 imessage_wrapped.py
```

It opens `http://127.0.0.1:8420` in your browser. Click **Generate my Wrapped**.

---

## What it tells you

The report reads as a chat thread — the numbers arrive as messages.

- **Your top conversation** — total messages, who sends more, how long each of you
  makes the other wait, your longest streak
- **The people** — everyone you talk to, ranked, with a search box for the whole
  address book and a full page for each person
- **When you text** — by hour of day and day of week
- **Month by month** — volume over your whole history
- **Emoji** — yours, theirs, and how often you use any at all
- **Reactions** — tapbacks given and received, and who tapbacks you most
- **Photos and files** — attachments by type
- **Your habits** — after-midnight messages, your most nocturnal contact, your busiest
  single day, your longest message
- **Calls** — phone and FaceTime, if this Mac has the history for it
- **Group chats** — ranked by how loud they are

You can build the whole history or a single year, and switch between years afterwards
without regenerating what you've already built.

## Requirements

- A Mac with Messages set up — macOS 13 or later for the app, older is fine for the
  script
- Full Disk Access, granted once (see below)

For the **app**, that's the whole list — it carries its own Python inside the bundle.

For the **script**, you also need Python 3.9 or newer. Modern macOS doesn't really ship
one: `/usr/bin/python3` is a stub that offers to install Apple's Command Line Tools the
first time you run it. Accept that prompt, or install Python from
[python.org](https://www.python.org/downloads/macos/). Either works.

Either way there is no `pip install`, no `requirements.txt`, and nothing to download.
The standard library is the whole dependency list.

## Full Disk Access

macOS protects `~/Library/Messages/`, so it has to be granted explicitly. Whichever way
you run this, you can revoke the permission again the moment you're done.

**In the app**, this is the second screen. Click **Open Privacy Settings**, switch
*iMessage Wrapped* on — it's already in the list — and the window carries on by itself.
The permission goes to the app alone, and to nothing else on your Mac.

**For the script**, the permission has to go to your terminal instead:

1. **System Settings → Privacy & Security → Full Disk Access**
2. Turn it on for **Terminal** (or iTerm, or whichever terminal you use)
3. **Quit the terminal completely** (⌘Q — closing the window isn't enough) and reopen it

Run `python3 imessage_wrapped.py --selftest` to confirm it worked. If it didn't, the
tool tells you exactly this in the browser rather than crashing.

Note that this is the broader of the two: a terminal with Full Disk Access passes it on
to everything you run from it. If that bothers you, use the app.

## Privacy

This is the entire point of the project, so it's worth being specific.

- **Nothing leaves your Mac.** The file contains no HTTP client — `urllib.parse` is
  imported for parsing query strings, `urllib.request` is not imported at all. There is
  no CDN, no font host, no analytics, no chart library. Every chart is hand-written SVG.
  It works with Wi-Fi off, and the page carries a `Content-Security-Policy` that blocks
  outbound requests even if that ever changed.
- **Your database is opened read-only.** Every connection uses SQLite's `mode=ro` URI.
  Nothing in `~/Library` is written, moved, or deleted.
- **The server is loopback-only.** It binds `127.0.0.1`, never `0.0.0.0`, and rejects
  any request whose `Host` header isn't `127.0.0.1` or `localhost` — so a malicious web
  page can't reach it by DNS rebinding while you have it open.
- **Message text is never written to disk.** The report holds counts, timestamps and
  contact names. Nothing is saved anywhere unless you click a Save button.
- **It stops when you stop it.** Ctrl+C — or quitting the app — shuts the server down
  and deletes any temporary database snapshot it made. The bundled engine also watches
  the app that launched it, so it can't be left running behind your back.

If your Messages database is mid-write (WAL mode), the tool copies it to a temp
directory to read it, and deletes that copy on exit. That's the only file it ever
creates on its own.

## Saving your report

Two buttons at the bottom:

- **Save a copy** — a single self-contained `.html` file with the report baked in. It
  opens from the Finder years from now with no Python, no server and no network.
- **Download the raw JSON** — every number the report is built from, if you'd rather
  do your own analysis.

Both are plain downloads to your Downloads folder. Nothing is uploaded either way. In
the app, the two buttons hand the file to your default browser, which saves it there the
same as any other download.

## Command line

```
python3 imessage_wrapped.py                    # start the app (the normal way)
python3 imessage_wrapped.py --selftest         # check permissions, schema, dates, text
python3 imessage_wrapped.py --dump-stats       # print the whole report as JSON
python3 imessage_wrapped.py --dump-stats --year 2025 --compact > 2025.json
python3 imessage_wrapped.py --port 9000        # serve somewhere else
python3 imessage_wrapped.py --no-browser       # print the URL, don't open anything
```

`--selftest` is the useful one if something looks wrong. It reports your Python and
macOS versions, the database size and schema variant, how many messages it found, how
many it could extract text from, and how many contacts it resolved to real names.

## The Mac app

The app is the same tool with a front door on it. Inside the bundle are two things: a
small native shell (SwiftUI/AppKit) that handles the permission screens and displays the
report, and the Python engine — the file at the root of this repo — frozen with its own
interpreter so nothing has to be installed. The shell starts the engine on a random
loopback port, reads that port back off its stdout, and points a web view at it. The
report you see is byte-for-byte the page the script serves.

The engine is a child process of the app, so the Full Disk Access you grant the app is
what it inherits — and it shuts itself down the moment the app quits.

### First launch

The release build is signed, but not notarised — notarising requires a paid Apple
Developer account, which this project doesn't have. So the first time you open it, macOS
will say it can't verify the developer. To get past that:

**System Settings → Privacy & Security**, scroll down to the message about *iMessage
Wrapped*, and click **Open Anyway**.

You only have to do this once. If you'd rather not take an unnotarised binary at its
word, build it yourself — it's one command — or just run the Python script instead.

### Building it yourself

```bash
./mac/build.sh
```

That produces `mac/dist/iMessage Wrapped.app` and a DMG beside it. See
[mac/README.md](mac/README.md) for what it needs and how to sign a build properly.

## How it works

`chat.db` is a SQLite database, and its schema has drifted across a decade of macOS
releases. The tool handles that rather than assuming:

- **Dates** are Apple Core Data timestamps (seconds since 2001-01-01 UTC), stored as
  seconds on old databases and nanoseconds on new ones. Both are detected and converted.
- **Message text** lives in `message.text` on older messages and in an
  `NSKeyedArchiver` blob (`attributedBody`) on newer ones. The typedstream format is
  decoded directly — no `pyobjc`, no dependencies. Self-test reports the extraction
  rate; it's typically above 99%.
- **Missing columns** on older macOS (`associated_message_emoji`, `is_audio_message`,
  `thread_originator_guid`) are detected with `PRAGMA table_info` and substituted with
  `NULL` rather than crashing the query.
- **Contacts** come from the AddressBook database when it's readable. When it isn't,
  everyone shows up as a formatted phone number and nothing breaks.
- **Identities are merged.** The same person texting from a phone number and an Apple
  ID is one person, not two.
- **Tapbacks are excluded** from message counts and counted separately as reactions.
  Group-chat system events are excluded. A message in several chats is counted once.
- **Emoji rankings are spam-resistant** — ranked by how many messages contain an emoji,
  not raw occurrences, so one message with 7,000 flags in it doesn't win.

## Limitations

- **macOS only.** There's no iPhone-only path — iOS sandboxes its own message database
  away from every other app, so Messages on your Mac has to have the history.
- **Only what this Mac has.** iCloud keeps older conversations on the server until
  something opens them. If your report starts later than you expected, that's why.
- **Contact names need the Contacts app.** Numbers you've never saved stay numbers.
- **Calls are best-effort.** `CallHistory.storedata` is undocumented Core Data and
  absent on plenty of Macs. When it can't be read, the Calls section just doesn't
  appear — no error, no gap.

## License

Copyright © 2026 Henry White. All rights reserved.

**Free to use.** Download it and run it on your own Mac as much as you like.

It is source-available, not open source. You may not redistribute it, sell it,
modify it, build on it, or use it commercially — those rights are reserved. See
[LICENSE](LICENSE) for the exact terms, and get in touch for a commercial or
redistribution licence.

The report it builds from your own messages is yours. The licence makes no claim
on it.
