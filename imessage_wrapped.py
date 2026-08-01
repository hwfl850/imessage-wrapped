#!/usr/bin/env python3
"""iMessage Wrapped — a local, offline report built from your own Messages history.

Single file. Python 3.9+ standard library only. No network access, ever.
Everything is read-only; nothing in ~/Library is modified.

Usage:
    python3 imessage_wrapped.py --selftest     # verify DB access, dates, text extraction
"""

from __future__ import annotations

import argparse
import atexit
import glob
import http.server
import json
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional

APP_NAME = "iMessage Wrapped"
MIN_PYTHON = (3, 9)

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
ADDRESSBOOK_GLOBS = (
    Path.home() / "Library" / "Application Support" / "AddressBook" / "AddressBook-v22.abcddb",
    Path.home()
    / "Library"
    / "Application Support"
    / "AddressBook"
    / "Sources"
    / "*"
    / "AddressBook-v22.abcddb",
)

# Apple's reference date: 2001-01-01 00:00:00 UTC, as a Unix timestamp.
APPLE_EPOCH = 978_307_200
# Above this magnitude the `date` column is nanoseconds (macOS 10.13+), below it seconds.
NANOSECOND_THRESHOLD = 1e11

STYLE_DIRECT = 45
STYLE_GROUP = 43

LARGE_DB_BYTES = 2 * 1024 * 1024 * 1024  # warn before copying anything bigger


# --------------------------------------------------------------------------------------
# Phase 0 — environment, permissions, database access
# --------------------------------------------------------------------------------------

FDA_INSTRUCTIONS = """
  ┌────────────────────────────────────────────────────────────────────────────┐
  │                                                                            │
  │  iMessage Wrapped needs permission to read your Messages database.         │
  │                                                                            │
  │  1. Open System Settings > Privacy & Security > Full Disk Access           │
  │  2. Click the + button                                                     │
  │  3. Add the app you are running this from:                                 │
  │       Terminal        /System/Applications/Utilities/Terminal.app          │
  │       iTerm2          /Applications/iTerm.app                              │
  │       VS Code         /Applications/Visual Studio Code.app                 │
  │  4. Quit that app completely (Cmd+Q — not just closing the window)         │
  │  5. Reopen it and run this script again                                    │
  │                                                                            │
  │  Nothing is uploaded. All processing happens on this Mac.                  │
  │                                                                            │
  └────────────────────────────────────────────────────────────────────────────┘
"""


class PreflightError(Exception):
    """The environment cannot support a run; carries a human-readable explanation."""


def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        have = ".".join(str(p) for p in sys.version_info[:3])
        need = ".".join(str(p) for p in MIN_PYTHON)
        raise PreflightError(
            f"{APP_NAME} needs Python {need} or newer, but this is Python {have}.\n"
            f"Try running it with:  python3 {Path(__file__).name}"
        )


def preflight() -> None:
    """Verify we can actually read the Messages database before doing anything else."""
    check_python_version()

    if sys.platform != "darwin":
        raise PreflightError(
            f"{APP_NAME} reads the macOS Messages database and only runs on a Mac."
        )

    if not CHAT_DB.exists():
        raise PreflightError(
            f"No Messages database found at {CHAT_DB}.\n"
            "Have you ever used Messages on this Mac while signed in?"
        )

    try:
        with open(CHAT_DB, "rb") as f:
            f.read(16)
    except PermissionError:
        raise PreflightError(FDA_INSTRUCTIONS)
    except OSError as exc:
        raise PreflightError(f"Could not read {CHAT_DB}: {exc}")


def _ro_uri(path: Path) -> str:
    """A read-only SQLite URI. Quoted, so a home directory containing '?' or '#' is
    read as part of the path and not as URI syntax."""
    return "file:" + urllib.parse.quote(str(path)) + "?mode=ro"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(_ro_uri(path), uri=True)
    con.row_factory = sqlite3.Row
    return con


_TEMP_DIRS: "list[Path]" = []


def cleanup_temp() -> int:
    """Delete every snapshot we made. Safe to call twice; returns how many were removed."""
    removed = 0
    while _TEMP_DIRS:
        tmp = _TEMP_DIRS.pop()
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
            removed += 1
    return removed


def _copy_db(src: Path, prefix: str) -> Path:
    """Copy a SQLite database and its WAL sidecars into a temp dir we can read freely."""
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    _TEMP_DIRS.append(tmp)
    atexit.register(shutil.rmtree, tmp, True)
    for suffix in ("", "-wal", "-shm"):
        sidecar = Path(str(src) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, tmp / sidecar.name)
    return tmp / src.name


def open_db(src: Optional[Path] = None, quiet: bool = False) -> sqlite3.Connection:
    """Open chat.db read-only, falling back to a temp copy when WAL recovery is needed."""
    src = src or CHAT_DB
    try:
        con = _connect_readonly(src)
        con.execute("SELECT ROWID FROM message LIMIT 1").fetchone()
        return con
    except sqlite3.DatabaseError:
        pass

    size = src.stat().st_size
    if size > LARGE_DB_BYTES and not quiet:
        print(
            f"  Messages is running, so a {size / 1024 / 1024 / 1024:.1f} GB snapshot is "
            "being copied. This takes a moment…",
            file=sys.stderr,
        )
    try:
        copied = _copy_db(src, "imw-")
    except PermissionError:
        # chat.db itself was readable but a -wal/-shm sidecar was not.
        raise PreflightError(FDA_INSTRUCTIONS)
    except OSError as exc:
        raise PreflightError(f"Could not snapshot the Messages database: {exc}")

    try:
        con = _connect_readonly(copied)
        con.execute("SELECT ROWID FROM message LIMIT 1").fetchone()
        return con
    except sqlite3.DatabaseError as exc:
        raise PreflightError(
            f"Could not open the Messages database ({exc}).\n"
            "Try quitting Messages (Cmd+Q) and running this again."
        )


# --------------------------------------------------------------------------------------
# Phase 1 — schema detection
# --------------------------------------------------------------------------------------

# Columns we reference that have genuinely disappeared/appeared across macOS versions.
OPTIONAL_COLUMNS = {
    "message": (
        "attributedBody",
        "associated_message_type",
        "associated_message_guid",
        "associated_message_emoji",
        "is_audio_message",
        "cache_has_attachments",
        "item_type",
        "is_empty",
        "date_edited",
        "balloon_bundle_id",
    ),
    "chat": ("display_name", "style", "service_name"),
    "attachment": ("mime_type", "uti", "is_sticker", "total_bytes", "transfer_name"),
}


@dataclass(frozen=True)
class Schema:
    """Which columns this particular chat.db actually has."""

    tables: "dict[str, frozenset]"

    @classmethod
    def detect(cls, con: sqlite3.Connection) -> "Schema":
        tables: "dict[str, frozenset]" = {}
        names = {
            row["name"]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in names:
            cols = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
            tables[table] = frozenset(cols)
        return cls(tables=tables)

    def has_table(self, table: str) -> bool:
        return table in self.tables

    def has(self, table: str, name: str) -> bool:
        return name in self.tables.get(table, frozenset())

    def col(self, table: str, name: str, alias: str, prefix: str, default: str = "NULL") -> str:
        """A SELECT-list fragment that degrades to a literal when the column is absent."""
        if self.has(table, name):
            return f"{prefix}.{name} AS {alias}"
        return f"{default} AS {alias}"

    def missing(self) -> "dict[str, tuple]":
        out: "dict[str, tuple]" = {}
        for table, cols in OPTIONAL_COLUMNS.items():
            absent = tuple(c for c in cols if not self.has(table, c))
            if absent:
                out[table] = absent
        return out


# --------------------------------------------------------------------------------------
# Phase 1 — dates
# --------------------------------------------------------------------------------------


def apple_to_unix(value: Optional[float]) -> Optional[float]:
    """Convert an Apple Core Data timestamp (seconds or nanoseconds) to Unix seconds."""
    if value is None:
        return None
    seconds = value / 1_000_000_000 if abs(value) > NANOSECOND_THRESHOLD else value
    return seconds + APPLE_EPOCH


def local_dt(unix: float) -> datetime:
    """Naive local-time datetime — late night means late night where the user was."""
    return datetime.fromtimestamp(unix)


# --------------------------------------------------------------------------------------
# Phase 1 — text extraction from attributedBody
# --------------------------------------------------------------------------------------

_NSSTRING = b"NSString"
# In a typedstream the string payload is introduced by the '+' type marker, and the
# length byte follows it immediately. Anchoring on '+' matters: a permissive "skip
# filler bytes" scan swallows the length of short messages ("ok" -> 0x02, "lol" -> 0x03).
_STRING_MARKER = 0x2B
_MARKER_WINDOW = 16
# Only used when no '+' marker turns up — class/object reference bytes.
_CLASS_REF_BYTES = frozenset({0x84, 0x85, 0x86, 0x92, 0x93, 0x94, 0x95, 0x96})
# If a decode drags in the next archived object, cut it off.
_TRAILING_MARKERS = ("NSDictionary", "NSNumber", "NSValue", "NSAttributedString", "NSObject")


def _read_length(blob: bytes, p: int) -> "tuple[Optional[int], int]":
    """Typedstream integer: a literal byte, or 0x81-0x83 followed by 2-4 LE bytes."""
    n = blob[p]
    p += 1
    if n == 0x81:
        return int.from_bytes(blob[p : p + 2], "little"), p + 2
    if n == 0x82:
        return int.from_bytes(blob[p : p + 3], "little"), p + 3
    if n == 0x83:
        return int.from_bytes(blob[p : p + 4], "little"), p + 4
    if n > 0x80:
        return None, p
    return n, p


def _length_positions(blob: bytes, start: int) -> "list[int]":
    """Where the string's length field might begin, best guess first."""
    p = start + len(_NSSTRING)
    positions: "list[int]" = []

    marker = blob.find(bytes([_STRING_MARKER]), p, p + _MARKER_WINDOW)
    if marker != -1:
        positions.append(marker + 1)

    q = p
    while q < len(blob) and blob[q] in _CLASS_REF_BYTES:
        q += 1
    if q not in positions:
        positions.append(q)
    return positions


def _extract_at(blob: bytes, start: int) -> "tuple[Optional[str], bool]":
    """Return (text, decoded_cleanly) for the string beginning at an NSString marker."""
    fallback: Optional[str] = None
    for p in _length_positions(blob, start):
        if p >= len(blob):
            continue
        length, p = _read_length(blob, p)
        if not length or p + length > len(blob):
            continue

        raw = blob[p : p + length]
        clean = True
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            clean = False
            text = raw.decode("utf-8", "ignore")

        for marker in _TRAILING_MARKERS:
            cut = text.find(marker)
            if cut != -1:
                text = text[:cut]
                clean = False
        text = text.rstrip("\x00")

        if text and clean:
            return text, True
        if text and fallback is None:
            fallback = text
    return fallback, False


def decode_attributed_body(blob: Optional[bytes]) -> Optional[str]:
    """Pull the plain string out of an NSKeyedArchiver typedstream blob.

    The archive can name NSString more than once (mutable subclasses, attribute runs);
    try every occurrence and prefer one that decodes as clean UTF-8.
    """
    if not blob:
        return None
    if isinstance(blob, memoryview):
        blob = blob.tobytes()

    fallback: Optional[str] = None
    start = blob.find(_NSSTRING)
    while start != -1:
        text, clean = _extract_at(blob, start)
        if text and clean:
            return text
        if text and fallback is None:
            fallback = text
        start = blob.find(_NSSTRING, start + 1)
    return fallback


def resolve_text(text: Optional[str], body: Optional[bytes]) -> Optional[str]:
    """`text` when present, otherwise the decoded attributedBody."""
    if text:
        return text
    return decode_attributed_body(body)


# --------------------------------------------------------------------------------------
# Phase 1 — identity: handles, contacts, people
# --------------------------------------------------------------------------------------


def normalize_handle(handle_id: Optional[str]) -> str:
    """Collapse the many spellings of one phone number/email into a single key."""
    if not handle_id:
        return ""
    s = handle_id.strip().lower()
    if "@" in s:
        return s
    digits = re.sub(r"\D", "", s)
    return digits[-10:] if len(digits) >= 10 else digits


def format_handle(handle_id: str) -> str:
    """Display fallback when Contacts has no name: (850) 555-1234."""
    if not handle_id:
        return "Unknown"
    if "@" in handle_id:
        return handle_id
    digits = re.sub(r"\D", "", handle_id)
    if len(digits) >= 10:
        d = digits[-10:]
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return handle_id


def _addressbook_paths() -> "list[Path]":
    paths: "list[Path]" = []
    for pattern in ADDRESSBOOK_GLOBS:
        for match in sorted(glob.glob(str(pattern))):
            p = Path(match)
            if p.exists():
                paths.append(p)
    return paths


def _record_name(row: sqlite3.Row) -> Optional[str]:
    first = (row["ZFIRSTNAME"] or "").strip()
    last = (row["ZLASTNAME"] or "").strip()
    name = " ".join(part for part in (first, last) if part)
    if name:
        return name
    org = (row["ZORGANIZATION"] or "").strip()
    return org or None


def load_contacts() -> "dict[str, str]":
    """Map normalized handle -> display name. Degrades to {} on any problem."""
    contacts: "dict[str, str]" = {}
    for db_path in _addressbook_paths():
        try:
            con = _connect_readonly(db_path)
        except sqlite3.DatabaseError:
            try:
                con = _connect_readonly(_copy_db(db_path, "imw-ab-"))
            except (sqlite3.DatabaseError, OSError):
                continue
        except OSError:
            continue

        try:
            names = {}
            for row in con.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"
            ):
                name = _record_name(row)
                if name:
                    names[row["Z_PK"]] = name

            for table, column in (
                ("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                ("ZABCDEMAILADDRESS", "ZADDRESS"),
            ):
                try:
                    rows = con.execute(f"SELECT {column} AS value, ZOWNER FROM {table}")
                except sqlite3.DatabaseError:
                    continue
                for row in rows:
                    key = normalize_handle(row["value"])
                    name = names.get(row["ZOWNER"])
                    if key and name:
                        contacts.setdefault(key, name)
        except sqlite3.DatabaseError:
            continue
        finally:
            con.close()

    return contacts


class Person(NamedTuple):
    person_id: str
    name: str
    handles: "tuple[str, ...]"
    is_contact: bool


def build_people(
    handles: Iterable[str], contacts: "dict[str, str]"
) -> "tuple[dict[str, Person], dict[str, str]]":
    """Group handles into people, merging the ones sharing a contact name.

    Returns (people by person_id, normalized handle -> person_id).
    """
    by_name: "dict[str, list[str]]" = defaultdict(list)
    unknown: "list[str]" = []
    seen: "set[str]" = set()

    for raw in handles:
        key = normalize_handle(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        name = contacts.get(key)
        if name:
            by_name[name].append(key)
        else:
            unknown.append(key)

    people: "dict[str, Person]" = {}
    handle_to_person: "dict[str, str]" = {}
    index = 0

    for name in sorted(by_name):
        index += 1
        pid = f"p_{index:04d}"
        keys = tuple(sorted(by_name[name]))
        people[pid] = Person(person_id=pid, name=name, handles=keys, is_contact=True)
        for key in keys:
            handle_to_person[key] = pid

    for key in sorted(unknown):
        index += 1
        pid = f"p_{index:04d}"
        people[pid] = Person(
            person_id=pid, name=format_handle(key), handles=(key,), is_contact=False
        )
        handle_to_person[key] = pid

    return people, handle_to_person


# --------------------------------------------------------------------------------------
# Phase 1 — loading messages
# --------------------------------------------------------------------------------------


class MessageRow(NamedTuple):
    mid: int
    ts: float
    from_me: bool
    text: str
    handle: str  # normalized; "" for messages we sent with no handle recorded
    chat_id: int
    chat_style: int
    chat_name: str
    chat_ident: str
    has_attachment: bool
    is_audio: bool
    service: str


class Tapback(NamedTuple):
    mid: int
    ts: float
    from_me: bool
    handle: str
    chat_id: int
    assoc_type: int
    assoc_guid: str
    emoji: str


@dataclass(frozen=True)
class Corpus:
    """Everything Phase 2 needs, loaded once."""

    messages: "tuple[MessageRow, ...]"
    tapbacks: "tuple[Tapback, ...]"
    people: "dict[str, Person]"
    handle_to_person: "dict[str, str]"
    contacts_resolved: bool
    stats: "dict[str, int]"
    # message guid -> was it mine; lets a tapback be traced to whose message it landed on
    guid_owner: "dict[str, bool]"


def _build_query(schema: Schema) -> str:
    fields = [
        "m.ROWID AS mid",
        "m.guid AS guid",
        "m.date AS raw_date",
        "m.is_from_me AS from_me",
        "m.text AS text",
        schema.col("message", "attributedBody", "body", "m"),
        schema.col("message", "service", "service", "m", default="''"),
        schema.col("message", "item_type", "item_type", "m", default="0"),
        schema.col("message", "is_empty", "is_empty", "m", default="0"),
        schema.col("message", "is_audio_message", "is_audio", "m", default="0"),
        schema.col("message", "cache_has_attachments", "has_att", "m", default="0"),
        schema.col("message", "associated_message_type", "assoc_type", "m", default="0"),
        schema.col("message", "associated_message_guid", "assoc_guid", "m", default="''"),
        schema.col("message", "associated_message_emoji", "assoc_emoji", "m", default="''"),
        "c.ROWID AS chat_id",
        schema.col("chat", "style", "chat_style", "c", default=str(STYLE_DIRECT)),
        schema.col("chat", "display_name", "chat_name", "c", default="''"),
        "c.chat_identifier AS chat_ident",
        "h.id AS handle_str",
    ]
    return (
        "SELECT\n    " + ",\n    ".join(fields) + "\n"
        "FROM message m\n"
        "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID\n"
        "JOIN chat c ON c.ROWID = cmj.chat_id\n"
        "LEFT JOIN handle h ON h.ROWID = m.handle_id\n"
        "ORDER BY c.ROWID, m.date"
    )


# Small enough that a typical library reports ~50 times, cheap enough that the callback
# never shows up in the profile. A coarser interval makes the progress bar jump.
PROGRESS_EVERY = 2_000


def year_bounds(year: int) -> "tuple[float, float]":
    """Local-time start and end of a calendar year, as Unix timestamps.

    Local, not UTC: a message sent at 11pm on New Year's Eve belongs to the year the
    sender was living in, not the one UTC happened to be in.
    """
    return datetime(year, 1, 1).timestamp(), datetime(year + 1, 1, 1).timestamp()


def load_corpus(
    con: sqlite3.Connection,
    schema: Schema,
    progress: Optional[Callable[[int], None]] = None,
    year: Optional[int] = None,
) -> Corpus:
    """One pass over chat.db, splitting real messages from tapbacks."""
    lo, hi = year_bounds(year) if year is not None else (float("-inf"), float("inf"))
    counters: Counter = Counter()
    messages: "list[MessageRow]" = []
    tapbacks: "list[Tapback]" = []
    handles: "set[str]" = set()
    guid_owner: "dict[str, bool]" = {}
    # A message can be joined to more than one chat; keep every row (Phase 2 needs the
    # per-chat context) but count text-resolution once per ROWID.
    counted: "set[int]" = set()

    for i, row in enumerate(con.execute(_build_query(schema)), start=1):
        counters["rows"] += 1
        if progress is not None and i % PROGRESS_EVERY == 0:
            progress(i)

        unix = apple_to_unix(row["raw_date"])
        if unix is None:
            counters["skipped_no_date"] += 1
            continue
        if not (lo <= unix < hi):
            counters["skipped_other_year"] += 1
            continue

        handle = normalize_handle(row["handle_str"])
        if handle:
            handles.add(handle)
        if row["guid"]:
            guid_owner[row["guid"]] = bool(row["from_me"])

        assoc_type = row["assoc_type"] or 0
        if assoc_type != 0:
            counters["tapback_rows"] += 1
            # 3000-series are tapback removals; Phase 2 filters them, keep them here.
            tapbacks.append(
                Tapback(
                    mid=row["mid"],
                    ts=unix,
                    from_me=bool(row["from_me"]),
                    handle=handle,
                    chat_id=row["chat_id"],
                    assoc_type=assoc_type,
                    assoc_guid=row["assoc_guid"] or "",
                    emoji=row["assoc_emoji"] or "",
                )
            )
            continue

        if (row["item_type"] or 0) != 0:
            counters["skipped_system_event"] += 1
            continue
        if row["is_empty"]:
            counters["skipped_empty_flag"] += 1
            continue

        raw_text = row["text"]
        body = row["body"]
        first_time = row["mid"] not in counted
        counted.add(row["mid"])
        if first_time:
            counters["candidates"] += 1

        if raw_text:
            text = raw_text
            if first_time:
                counters["from_text"] += 1
        elif body:
            text = decode_attributed_body(body) or ""
            if first_time:
                counters["body_only"] += 1
                counters["from_body" if text else "body_failed"] += 1
        else:
            text = ""
            if first_time:
                counters["no_content"] += 1

        has_att = bool(row["has_att"])
        if not text.strip() and not has_att:
            counters["skipped_no_text"] += 1
            continue

        messages.append(
            MessageRow(
                mid=row["mid"],
                ts=unix,
                from_me=bool(row["from_me"]),
                text=text,
                handle=handle,
                chat_id=row["chat_id"],
                chat_style=row["chat_style"] if row["chat_style"] is not None else STYLE_DIRECT,
                chat_name=row["chat_name"] or "",
                chat_ident=row["chat_ident"] or "",
                has_attachment=has_att,
                is_audio=bool(row["is_audio"]),
                service=row["service"] or "",
            )
        )

    if progress is not None:
        progress(counters["rows"])

    contacts = load_contacts()
    people, handle_to_person = build_people(handles, contacts)

    counters["messages"] = len(messages)
    counters["distinct_messages"] = len({m.mid for m in messages})
    counters["tapbacks"] = len(tapbacks)
    counters["handles"] = len(handles)
    counters["people"] = len(people)
    counters["contacts_in_addressbook"] = len(contacts)
    counters["people_named"] = sum(1 for p in people.values() if p.is_contact)

    return Corpus(
        messages=tuple(messages),
        tapbacks=tuple(tapbacks),
        people=people,
        handle_to_person=handle_to_person,
        contacts_resolved=bool(contacts),
        stats=dict(counters),
        guid_owner=guid_owner,
    )


# --------------------------------------------------------------------------------------
# Phase 2 — the statistics engine
# --------------------------------------------------------------------------------------

# A message opens a conversation if nothing was said in that chat for this long.
CONVERSATION_GAP_SECONDS = 4 * 60 * 60
# Beyond this a "reply" is really a new conversation; excluded from reply averages
# (but preserved for the longest-ghost stat).
REPLY_WINDOW_SECONDS = 24 * 60 * 60
# Sub-second gaps are delivery artifacts, not human speed.
MIN_FAST_REPLY_SECONDS = 1.0
LATE_NIGHT_END_HOUR = 5  # 00:00–04:59 local
NOCTURNAL_MIN_MESSAGES = 50
TRIM_FRACTION = 0.1
TOP_PEOPLE = 10
TOP_EMOJI = 10
TOP_REACTORS = 5
WEEKS_CHARTED = 52
SMALL_HISTORY_THRESHOLD = 100

TAPBACK_NAMES = {
    2000: "Loved",
    2001: "Liked",
    2002: "Disliked",
    2003: "Laughed",
    2004: "Emphasized",
    2005: "Questioned",
}
EMOJI_TAPBACK_LABEL = "Emoji reactions"
TAPBACK_REMOVED_BASE = 3000

DOW_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_EMOJI_BASE = (
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF]"
)
_SKIN_TONE = "[\U0001F3FB-\U0001F3FF]"
_VS16 = "️"
_ZWJ = "‍"
# A flag is a *pair* of regional indicators — matched first so 🇺🇸 never splits into
# 🇺 + 🇸. Skin tones and ZWJ sequences stay attached to their base, and the variation
# selector is only ever a suffix, so it can never match as an emoji by itself.
_EMOJI_UNIT = _EMOJI_BASE + _SKIN_TONE + "?" + _VS16 + "?"
EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF]{2}"
    "|" + _EMOJI_UNIT + "(?:" + _ZWJ + _EMOJI_UNIT + ")*"
)
# Modifiers are never an emoji on their own.
_EMOJI_MODIFIERS = frozenset(
    [_VS16, _ZWJ] + [chr(c) for c in range(0x1F3FB, 0x1F400)]
)

BIRTHDAY_RE = re.compile(r"happy\s+b[- ]?day|happy\s+birthday|hbd\b", re.I)


def _median(values: "list[float]") -> Optional[float]:
    """A true median — never a mean."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _trimmed_mean(values: "list[float]", trim: float = TRIM_FRACTION) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    cut = int(n * trim)
    core = ordered[cut : n - cut] if n - 2 * cut > 0 else ordered
    return sum(core) / len(core)


def _mean(values: "list[float]") -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def _round(value: Optional[float], places: int = 2) -> Optional[float]:
    return None if value is None else round(float(value), places)


def _pairs(counter: Counter, limit: int) -> "list[list]":
    """most_common as lists, so the dict matches its JSON round-trip exactly."""
    return [[key, count] for key, count in counter.most_common(limit)]


def _iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else local_dt(ts).isoformat(timespec="seconds")


def _day(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else local_dt(ts).strftime("%Y-%m-%d")


def _longest_streak(days: "set") -> "tuple[int, Optional[str], Optional[str]]":
    if not days:
        return 0, None, None
    ordered = sorted(days)
    best_len, best_start, best_end = 1, ordered[0], ordered[0]
    run_len, run_start = 1, ordered[0]
    for prev, cur in zip(ordered, ordered[1:]):
        if (cur - prev).days == 1:
            run_len += 1
        else:
            run_len, run_start = 1, cur
        if run_len > best_len:
            best_len, best_start, best_end = run_len, run_start, cur
    return best_len, best_start.isoformat(), best_end.isoformat()


def _month_keys(start: datetime, end: datetime) -> "list[str]":
    """Every month between two dates inclusive, so the line chart has no holes."""
    keys: "list[str]" = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return keys


def _week_keys(end: datetime, weeks: int = WEEKS_CHARTED) -> "list[str]":
    monday = (end - timedelta(days=end.weekday())).date()
    return [(monday - timedelta(weeks=w)).isoformat() for w in range(weeks - 1, -1, -1)]


def attachment_bucket(mime: Optional[str], uti: Optional[str], is_sticker: object) -> str:
    """Sticker wins over mime type — a sticker is usually an image/png but isn't a photo."""
    if is_sticker:
        return "Stickers"
    mime = (mime or "").lower()
    uti = (uti or "").lower()
    if mime == "image/gif" or "gif" in uti:
        return "GIFs"
    if mime.startswith("image/"):
        return "Photos"
    if mime.startswith("video/"):
        return "Videos"
    if mime.startswith("audio/"):
        return "Audio"
    if not mime and uti:
        if "image" in uti:
            return "Photos"
        if "movie" in uti or "video" in uti:
            return "Videos"
    return "Files"


def load_attachments(con: sqlite3.Connection, schema: Schema) -> "dict[int, list[str]]":
    """message ROWID -> attachment buckets. Counts only; files are never opened."""
    if not (schema.has_table("message_attachment_join") and schema.has_table("attachment")):
        return {}
    fields = [
        "maj.message_id AS mid",
        schema.col("attachment", "mime_type", "mime", "a", default="''"),
        schema.col("attachment", "uti", "uti", "a", default="''"),
        schema.col("attachment", "is_sticker", "is_sticker", "a", default="0"),
    ]
    sql = (
        "SELECT " + ", ".join(fields) + " FROM message_attachment_join maj "
        "JOIN attachment a ON a.ROWID = maj.attachment_id"
    )
    out: "dict[int, list[str]]" = defaultdict(list)
    try:
        for row in con.execute(sql):
            out[row["mid"]].append(
                attachment_bucket(row["mime"], row["uti"], row["is_sticker"])
            )
    except sqlite3.DatabaseError:
        return {}
    return dict(out)


def load_group_sizes(con: sqlite3.Connection, schema: Schema) -> "dict[int, int]":
    if not schema.has_table("chat_handle_join"):
        return {}
    try:
        return {
            row["chat_id"]: row["n"]
            for row in con.execute(
                "SELECT chat_id, COUNT(*) AS n FROM chat_handle_join GROUP BY chat_id"
            )
        }
    except sqlite3.DatabaseError:
        return {}


def extract_emoji(text: str) -> "list[str]":
    """Emoji in a message, kept whole: flags paired, skin tones and ZWJ joins attached.

    The variation selector is kept so ❤️ renders as an emoji rather than a text glyph;
    matches that are nothing but modifiers are dropped.
    """
    found: "list[str]" = []
    for match in EMOJI_RE.findall(text):
        stripped = match.strip(_ZWJ)
        if stripped and not all(ch in _EMOJI_MODIFIERS for ch in stripped):
            found.append(stripped)
    return found


def target_guid(assoc_guid: str) -> str:
    """`p:0/<guid>` or `bp:<guid>` -> `<guid>`."""
    if not assoc_guid:
        return ""
    if "/" in assoc_guid:
        return assoc_guid.rsplit("/", 1)[-1]
    if ":" in assoc_guid:
        return assoc_guid.rsplit(":", 1)[-1]
    return assoc_guid


class StatAccumulator:
    """One scope's running totals: global, one person, or one group chat."""

    def __init__(self, dedupe: bool = False) -> None:
        # Only the global scopes need this: a message joined to two chats must count once.
        self._seen: "Optional[set[int]]" = set() if dedupe else None

        self.sent = 0
        self.received = 0
        self.persons: "set[str]" = set()
        self.chats: "set[int]" = set()
        self.first_ts: Optional[float] = None
        self.last_ts: Optional[float] = None

        self.days: "set" = set()
        self.day_counts: Counter = Counter()
        self.dow = [0] * 7
        self.hours = [0] * 24
        self.months: Counter = Counter()
        self.weeks: Counter = Counter()

        self.len_sent: "list[int]" = []
        self.len_received: "list[int]" = []
        self.longest: "Optional[tuple[int, float, bool]]" = None

        self.reply_me: "list[float]" = []
        self.reply_them: "list[float]" = []
        self.ghost_me: "Optional[tuple[float, float]]" = None
        self.ghost_them: "Optional[tuple[float, float]]" = None
        self.fastest: "Optional[tuple[float, float, Optional[str]]]" = None

        self.started_me = 0
        self.started_them = 0

        self.emoji_me: Counter = Counter()
        self.emoji_them: Counter = Counter()
        # Raw occurrences let one spam message ("🇺🇸" × 7,585 in a single text) decide the
        # whole ranking, so also count how many messages each emoji appeared in.
        self.emoji_me_msgs: Counter = Counter()
        self.emoji_them_msgs: Counter = Counter()

        self.attachments: Counter = Counter()
        self.voice_notes = 0

        self.late_night = 0
        self.birthday_sent = 0
        self.birthday_received = 0

        self.tapbacks_given: Counter = Counter()
        self.tapbacks_received: Counter = Counter()
        self.tapback_emoji: Counter = Counter()
        self.reactors: Counter = Counter()

    # -- ingestion ---------------------------------------------------------------------

    def ingest(
        self,
        m: MessageRow,
        dt: datetime,
        person_id: Optional[str],
        buckets: "Iterable[str]",
        reply_delta: Optional[float],
        starts_conversation: bool,
    ) -> None:
        if self._seen is not None:
            if m.mid in self._seen:
                return
            self._seen.add(m.mid)

        if m.from_me:
            self.sent += 1
        else:
            self.received += 1
        if person_id:
            self.persons.add(person_id)
        self.chats.add(m.chat_id)

        if self.first_ts is None or m.ts < self.first_ts:
            self.first_ts = m.ts
        if self.last_ts is None or m.ts > self.last_ts:
            self.last_ts = m.ts

        day = dt.date()
        self.days.add(day)
        self.day_counts[day.isoformat()] += 1
        self.dow[dt.weekday()] += 1
        self.hours[dt.hour] += 1
        self.months[f"{dt.year:04d}-{dt.month:02d}"] += 1
        self.weeks[(day - timedelta(days=dt.weekday())).isoformat()] += 1
        if dt.hour < LATE_NIGHT_END_HOUR:
            self.late_night += 1

        text = m.text
        if text:
            length = len(text)
            (self.len_sent if m.from_me else self.len_received).append(length)
            if self.longest is None or length > self.longest[0]:
                self.longest = (length, m.ts, bool(m.from_me))

            emoji = extract_emoji(text)
            if emoji:
                if m.from_me:
                    self.emoji_me.update(emoji)
                    self.emoji_me_msgs.update(set(emoji))
                else:
                    self.emoji_them.update(emoji)
                    self.emoji_them_msgs.update(set(emoji))
            if BIRTHDAY_RE.search(text):
                if m.from_me:
                    self.birthday_sent += 1
                else:
                    self.birthday_received += 1

        for bucket in buckets:
            self.attachments[bucket] += 1
        if m.is_audio:
            self.voice_notes += 1

        if starts_conversation:
            if m.from_me:
                self.started_me += 1
            else:
                self.started_them += 1

        if reply_delta is not None:
            self._add_reply(reply_delta, bool(m.from_me), m.ts, person_id)

    def _add_reply(
        self, delta: float, by_me: bool, ts: float, person_id: Optional[str]
    ) -> None:
        # The raw max feeds the ghost stats; only sub-24h deltas feed the averages.
        slot = "ghost_me" if by_me else "ghost_them"
        current = getattr(self, slot)
        if current is None or delta > current[0]:
            setattr(self, slot, (delta, ts))

        if delta > REPLY_WINDOW_SECONDS:
            return
        (self.reply_me if by_me else self.reply_them).append(delta)

        if by_me and delta >= MIN_FAST_REPLY_SECONDS:
            if self.fastest is None or delta < self.fastest[0]:
                self.fastest = (delta, ts, person_id)

    def ingest_tapback(self, kind: str, emoji: str, given: bool, reactor: Optional[str]) -> None:
        if given:
            self.tapbacks_given[kind] += 1
        else:
            self.tapbacks_received[kind] += 1
            if reactor:
                self.reactors[reactor] += 1
        if emoji:
            self.tapback_emoji[emoji] += 1

    # -- output ------------------------------------------------------------------------

    def total(self) -> int:
        return self.sent + self.received

    def finalize(self, people: "dict[str, Person]") -> dict:
        total = self.total()
        lengths = self.len_sent + self.len_received

        busiest_day = self.day_counts.most_common(1)
        peak_hour = max(range(24), key=lambda h: self.hours[h]) if total else None
        peak_dow = max(range(7), key=lambda d: self.dow[d]) if total else None
        streak_days, streak_start, streak_end = _longest_streak(self.days)

        monthly: "list[dict]" = []
        weekly: "list[dict]" = []
        if self.first_ts is not None and self.last_ts is not None:
            first_dt, last_dt = local_dt(self.first_ts), local_dt(self.last_ts)
            monthly = [
                {"month": key, "count": self.months.get(key, 0)}
                for key in _month_keys(first_dt, last_dt)
            ]
            weekly = [
                {"week": key, "count": self.weeks.get(key, 0)}
                for key in _week_keys(last_dt)
            ]

        days_covered = None
        if self.first_ts is not None and self.last_ts is not None:
            days_covered = (local_dt(self.last_ts).date() - local_dt(self.first_ts).date()).days + 1

        def person_name(pid: Optional[str]) -> Optional[str]:
            person = people.get(pid) if pid else None
            return person.name if person else None

        fastest = None
        if self.fastest:
            delta, ts, pid = self.fastest
            fastest = {
                "seconds": _round(delta),
                "date": _iso(ts),
                "person_id": pid,
                "name": person_name(pid),
            }

        return {
            "totals": {
                "sent": self.sent,
                "received": self.received,
                "total": total,
                "ratio": _round(self.sent / self.received) if self.received else None,
                "people": len(self.persons),
                "chats": len(self.chats),
                "first_message": _iso(self.first_ts),
                "last_message": _iso(self.last_ts),
                "days_covered": days_covered,
                "active_days": len(self.days),
                "sparse": total < SMALL_HISTORY_THRESHOLD,
            },
            "reply": {
                "you_median_seconds": _round(_median(self.reply_me)),
                "them_median_seconds": _round(_median(self.reply_them)),
                "you_trimmed_mean_seconds": _round(_trimmed_mean(self.reply_me)),
                "them_trimmed_mean_seconds": _round(_trimmed_mean(self.reply_them)),
                "you_replies": len(self.reply_me),
                "them_replies": len(self.reply_them),
                "fastest": fastest,
                "longest_wait_before_you_replied": (
                    {"seconds": _round(self.ghost_me[0]), "date": _iso(self.ghost_me[1])}
                    if self.ghost_me
                    else None
                ),
                "longest_they_left_you": (
                    {"seconds": _round(self.ghost_them[0]), "date": _iso(self.ghost_them[1])}
                    if self.ghost_them
                    else None
                ),
            },
            "length": {
                "you_mean": _round(_mean(self.len_sent), 1),
                "you_median": _round(_median(self.len_sent), 1),
                "them_mean": _round(_mean(self.len_received), 1),
                "them_median": _round(_median(self.len_received), 1),
                "overall_mean": _round(_mean(lengths), 1),
                "overall_median": _round(_median(lengths), 1),
                "longest": (
                    # Length and date only — the message body is deliberately not exposed.
                    {
                        "chars": self.longest[0],
                        "date": _iso(self.longest[1]),
                        "from_me": self.longest[2],
                    }
                    if self.longest
                    else None
                ),
            },
            "days": {
                "busiest_date": (
                    {"date": busiest_day[0][0], "count": busiest_day[0][1]}
                    if busiest_day
                    else None
                ),
                "day_of_week": [
                    {"day": DOW_NAMES[i], "count": self.dow[i]} for i in range(7)
                ],
                "busiest_day_of_week": DOW_NAMES[peak_dow] if peak_dow is not None else None,
            },
            "hours": {
                "histogram": [{"hour": h, "count": self.hours[h]} for h in range(24)],
                "peak_hour": peak_hour,
                "peak_count": self.hours[peak_hour] if peak_hour is not None else 0,
            },
            "streak": {"days": streak_days, "start": streak_start, "end": streak_end},
            "conversations": {
                "you_started": self.started_me,
                "they_started": self.started_them,
                "total": self.started_me + self.started_them,
            },
            "volume": {"monthly": monthly, "weekly": weekly},
            "attachments": {
                "buckets": dict(self.attachments),
                "voice_notes": self.voice_notes,
                "total": sum(self.attachments.values()) + self.voice_notes,
            },
            "tapbacks": {
                "given": dict(self.tapbacks_given),
                "received": dict(self.tapbacks_received),
                "given_total": sum(self.tapbacks_given.values()),
                "received_total": sum(self.tapbacks_received.values()),
                "custom_emoji": _pairs(self.tapback_emoji, TOP_EMOJI),
                "top_reactors": [
                    {"person_id": pid, "name": person_name(pid), "count": n}
                    for pid, n in self.reactors.most_common(TOP_REACTORS)
                ],
            },
            "emoji": {
                # Occurrence counts — accurate, but skewed by any single spam message.
                "you": _pairs(self.emoji_me, TOP_EMOJI),
                "them": _pairs(self.emoji_them, TOP_EMOJI),
                "combined": _pairs(self.emoji_me + self.emoji_them, TOP_EMOJI),
                # Messages the emoji appeared in — the spam-proof ranking to display.
                "you_by_message": _pairs(self.emoji_me_msgs, TOP_EMOJI),
                "them_by_message": _pairs(self.emoji_them_msgs, TOP_EMOJI),
                "combined_by_message": _pairs(
                    self.emoji_me_msgs + self.emoji_them_msgs, TOP_EMOJI
                ),
                "you_total": sum(self.emoji_me.values()),
                "them_total": sum(self.emoji_them.values()),
                "you_messages_with_emoji": sum(self.emoji_me_msgs.values()),
                "them_messages_with_emoji": sum(self.emoji_them_msgs.values()),
            },
            "late_night": {
                "count": self.late_night,
                "share": _round(self.late_night / total * 100, 1) if total else None,
            },
            "birthday": {"sent": self.birthday_sent, "received": self.birthday_received},
        }


def _chat_person(msgs: "list[MessageRow]", corpus: Corpus) -> Optional[str]:
    """Which person a 1:1 chat belongs to.

    Messages we sent carry no handle, so the chat identifier is the primary signal.
    """
    pid = corpus.handle_to_person.get(normalize_handle(msgs[0].chat_ident))
    if pid:
        return pid
    counts = Counter(m.handle for m in msgs if m.handle)
    for handle, _ in counts.most_common():
        pid = corpus.handle_to_person.get(handle)
        if pid:
            return pid
    return None


YEAR_FLOOR = 25  # a year with fewer messages than this isn't worth offering as a filter


def available_years(con: sqlite3.Connection) -> "list[int]":
    """Calendar years worth offering as a filter, newest first.

    Years are counted rather than interpolated between the first and last message: a
    library that starts in 2011 and goes quiet until 2022 shouldn't offer a decade of
    chips that all lead to an empty report. Timestamps are converted in Python instead
    of SQL because the date column is seconds on old databases and nanoseconds on new
    ones, and apple_to_unix already knows the difference.
    """
    try:
        rows = con.execute("SELECT date FROM message WHERE date != 0").fetchall()
    except sqlite3.DatabaseError:
        return []
    counts: Counter = Counter()
    for row in rows:
        unix = apple_to_unix(row["date"])
        if unix is not None:
            counts[local_dt(unix).year] += 1
    return sorted(
        (y for y, n in counts.items() if n >= YEAR_FLOOR and 1990 < y < 2100), reverse=True
    )


# --------------------------------------------------------------------------------------
# Phase 5 — call history (optional; the database may not exist at all)
# --------------------------------------------------------------------------------------

CALL_DB = (
    Path.home() / "Library" / "Application Support" / "CallHistoryDB" / "CallHistory.storedata"
)


def load_calls(corpus: Corpus, year: Optional[int] = None) -> Optional[dict]:
    """Summarise FaceTime and phone calls, or return None if there's nothing to show.

    Everything here is best-effort: the database is absent on plenty of Macs, its schema
    is Core Data's and undocumented, and no stat in the report depends on it. Any failure
    just means the Calls section doesn't appear.
    """
    if not CALL_DB.exists():
        return None
    con = None
    try:
        try:
            con = _connect_readonly(CALL_DB)
            con.execute("SELECT ROWID FROM ZCALLRECORD LIMIT 1").fetchone()
        except sqlite3.DatabaseError:
            con = _connect_readonly(_copy_db(CALL_DB, "imw-calls-"))

        cols = {r["name"] for r in con.execute("PRAGMA table_info(ZCALLRECORD)")}
        if not {"ZDATE", "ZDURATION"} <= cols:
            return None
        answered = "ZANSWERED" if "ZANSWERED" in cols else "NULL"
        originated = "ZORIGINATED" if "ZORIGINATED" in cols else "NULL"
        address = "ZADDRESS" if "ZADDRESS" in cols else "NULL"
        rows = con.execute(
            f"SELECT ZDATE AS d, ZDURATION AS dur, {answered} AS answered,"
            f" {originated} AS out, {address} AS addr FROM ZCALLRECORD"
        ).fetchall()
    except (sqlite3.DatabaseError, OSError, PreflightError):
        return None
    finally:
        if con is not None:
            con.close()

    lo, hi = year_bounds(year) if year is not None else (float("-inf"), float("inf"))
    total = outgoing = incoming = missed = 0
    seconds = 0.0
    longest = {"seconds": 0.0, "date": None, "name": None}
    per_person: Counter = Counter()
    hours: Counter = Counter()

    for r in rows:
        unix = apple_to_unix(r["d"])
        if unix is None or not (lo <= unix < hi):
            continue
        total += 1
        dur = float(r["dur"] or 0)
        seconds += dur
        hours[local_dt(unix).hour] += 1
        if r["out"]:
            outgoing += 1
        else:
            incoming += 1
        if r["answered"] is not None and not r["answered"] and not r["out"]:
            missed += 1

        # ZADDRESS is usually a phone number, sometimes stored as bytes.
        raw = r["addr"]
        if isinstance(raw, (bytes, bytearray, memoryview)):
            raw = bytes(raw).decode("utf-8", "ignore")
        name = None
        if raw:
            pid = corpus.handle_to_person.get(normalize_handle(str(raw)))
            person = corpus.people.get(pid) if pid else None
            name = person.name if person else format_handle(str(raw))
            per_person[name] += 1
        if dur > longest["seconds"]:
            longest = {"seconds": _round(dur, 1), "date": _iso(unix), "name": name}

    if total == 0:
        return None
    return {
        "total": total,
        "outgoing": outgoing,
        "incoming": incoming,
        "missed": missed,
        "total_seconds": _round(seconds, 1),
        "mean_seconds": _round(seconds / total, 1),
        "longest": longest if longest["date"] else None,
        "top_people": _pairs(per_person, 8),
        "peak_hour": hours.most_common(1)[0][0] if hours else None,
    }


def build_report(
    corpus: Corpus,
    con: sqlite3.Connection,
    schema: Schema,
    progress: "Optional[Callable[[str, float], None]]" = None,
    year: Optional[int] = None,
    years: "Optional[list[int]]" = None,
) -> dict:
    """Turn a loaded corpus into the full report JSON. One pass over the messages."""

    def step(name: str, fraction: float = 0.0) -> None:
        if progress is not None:
            progress(name, fraction)

    step("Reading attachments")
    attachments = load_attachments(con, schema)
    group_sizes = load_group_sizes(con, schema)

    step("Crunching numbers")
    everything = StatAccumulator(dedupe=True)
    direct_only = StatAccumulator(dedupe=True)
    per_person: "dict[str, StatAccumulator]" = {}
    groups: "dict[int, StatAccumulator]" = {}
    chat_person: "dict[int, Optional[str]]" = {}
    group_names: "dict[int, str]" = {}

    # This loop is the bulk of the work, so report against messages consumed rather
    # than letting the bar sit still for its whole duration.
    total_rows = len(corpus.messages) or 1
    done_rows = 0
    next_tick = PROGRESS_EVERY

    for chat_id, grouped in groupby(corpus.messages, key=lambda m: m.chat_id):
        msgs = list(grouped)
        done_rows += len(msgs)
        if done_rows >= next_tick:
            step("Crunching numbers", done_rows / total_rows)
            next_tick = done_rows + PROGRESS_EVERY
        is_direct = msgs[0].chat_style == STYLE_DIRECT
        person_id = _chat_person(msgs, corpus) if is_direct else None
        chat_person[chat_id] = person_id

        targets = [everything]
        if is_direct:
            targets.append(direct_only)
            if person_id:
                targets.append(per_person.setdefault(person_id, StatAccumulator()))
        else:
            targets.append(groups.setdefault(chat_id, StatAccumulator()))
            group_names[chat_id] = msgs[0].chat_name or msgs[0].chat_ident

        previous: Optional[MessageRow] = None
        for m in msgs:
            dt = local_dt(m.ts)
            gap = None if previous is None else m.ts - previous.ts
            starts = gap is None or gap > CONVERSATION_GAP_SECONDS
            # A reply is a direction flip; negative gaps are clock skew.
            reply_delta = (
                gap
                if previous is not None and previous.from_me != m.from_me and gap > 0
                else None
            )
            buckets = attachments.get(m.mid, ())
            # In a group chat the sender varies per message, so resolve per row; that
            # way the global "distinct people" count includes group-only contacts.
            msg_person = person_id if is_direct else corpus.handle_to_person.get(m.handle)
            for acc in targets:
                acc.ingest(m, dt, msg_person, buckets, reply_delta, starts)
            previous = m

    step("Reading reactions")
    seen_tapbacks: "set[int]" = set()
    total_tb = len(corpus.tapbacks) or 1
    for i, tb in enumerate(corpus.tapbacks, start=1):
        if i % PROGRESS_EVERY == 0:
            step("Reading reactions", i / total_tb)
        if TAPBACK_REMOVED_BASE <= tb.assoc_type < TAPBACK_REMOVED_BASE + 1000:
            continue  # a removed tapback, not a reaction
        kind = TAPBACK_NAMES.get(tb.assoc_type, EMOJI_TAPBACK_LABEL)
        given = tb.from_me
        # Only count a received tapback as "someone reacted to you" when it really
        # landed on one of your messages.
        on_my_message = corpus.guid_owner.get(target_guid(tb.assoc_guid))
        reactor = corpus.handle_to_person.get(tb.handle) if on_my_message else None

        person_id = chat_person.get(tb.chat_id)
        first_time = tb.mid not in seen_tapbacks
        seen_tapbacks.add(tb.mid)

        if first_time:
            everything.ingest_tapback(kind, tb.emoji, given, reactor)
        if person_id and person_id in per_person:
            if first_time:
                direct_only.ingest_tapback(kind, tb.emoji, given, reactor)
            per_person[person_id].ingest_tapback(kind, tb.emoji, given, reactor)
        elif tb.chat_id in groups:
            groups[tb.chat_id].ingest_tapback(kind, tb.emoji, given, reactor)

    step("Building your Wrapped")
    people_blocks = []
    for pid, acc in sorted(per_person.items(), key=lambda kv: -kv[1].total()):
        person = corpus.people.get(pid)
        if person is None or acc.total() == 0:
            continue
        people_blocks.append(
            {
                "person_id": pid,
                "name": person.name,
                "handles": list(person.handles),
                "is_contact": person.is_contact,
                "total": acc.total(),
                "sent": acc.sent,
                "received": acc.received,
                # Broadcast senders (shortcodes, delivery alerts) receive nothing back.
                # Phase 4 needs this to keep them out of the "top conversation" headline.
                "two_way": acc.sent > 0 and acc.received > 0,
                "stats": acc.finalize(corpus.people),
            }
        )

    global_block = everything.finalize(corpus.people)
    direct_block = direct_only.finalize(corpus.people)

    global_block["top_people"] = [
        {
            "person_id": p["person_id"],
            "name": p["name"],
            "total": p["total"],
            "sent": p["sent"],
            "received": p["received"],
            "two_way": p["two_way"],
        }
        for p in people_blocks[:TOP_PEOPLE]
    ]

    # Most nocturnal contact, with a floor so one 2am text can't win.
    nocturnal = None
    for p in people_blocks:
        acc = per_person[p["person_id"]]
        if acc.total() < NOCTURNAL_MIN_MESSAGES:
            continue
        share = acc.late_night / acc.total() * 100
        if nocturnal is None or share > nocturnal["share"]:
            nocturnal = {
                "person_id": p["person_id"],
                "name": p["name"],
                "share": _round(share, 1),
                "count": acc.late_night,
                "total": acc.total(),
            }
    global_block["late_night"]["most_nocturnal"] = nocturnal
    direct_block["late_night"]["most_nocturnal"] = nocturnal

    group_blocks = [
        {
            "chat_id": chat_id,
            "name": group_names.get(chat_id) or f"Group chat {chat_id}",
            "total": acc.total(),
            "sent": acc.sent,
            "received": acc.received,
            "members": group_sizes.get(chat_id, 0),
            "first_message": _iso(acc.first_ts),
            "last_message": _iso(acc.last_ts),
        }
        for chat_id, acc in sorted(groups.items(), key=lambda kv: -kv[1].total())
        if acc.total() > 0
    ]

    try:
        db_size_mb = round(CHAT_DB.stat().st_size / 1024 / 1024)
    except OSError:
        db_size_mb = None

    calls_block = load_calls(corpus, year)
    if years is None:
        years = available_years(con)

    return {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "app": APP_NAME,
            "first_message": global_block["totals"]["first_message"],
            "last_message": global_block["totals"]["last_message"],
            "days_covered": global_block["totals"]["days_covered"],
            "contacts_resolved": corpus.contacts_resolved,
            "people_named": sum(1 for p in corpus.people.values() if p.is_contact),
            "db_size_mb": db_size_mb,
            "conversation_gap_hours": CONVERSATION_GAP_SECONDS // 3600,
            "year": year,
            "years_available": years,
        },
        "global": global_block,
        "global_direct": direct_block,
        "people": people_blocks,
        "groups": group_blocks,
        "calls": calls_block,
    }


# --------------------------------------------------------------------------------------
# Self test
# --------------------------------------------------------------------------------------

TEXT_RESOLUTION_FLOOR = 95.0


def _rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def _line(label: str, value: str) -> None:
    print(f"  {label:<28} {value}")


def selftest() -> int:
    print(f"\n{APP_NAME} — self test")
    problems: "list[str]" = []

    _rule("Environment")
    _line("Python", ".".join(str(p) for p in sys.version_info[:3]))
    _line("Platform", sys.platform)

    preflight()
    size_mb = CHAT_DB.stat().st_size / 1024 / 1024
    _line("chat.db", str(CHAT_DB))
    _line("Size", f"{size_mb:,.0f} MB")

    con = open_db()
    using_copy = "Library/Messages" not in (
        con.execute("PRAGMA database_list").fetchone()["file"] or ""
    )
    _line("Access mode", "temp snapshot (WAL)" if using_copy else "direct read-only")

    _rule("Schema")
    schema = Schema.detect(con)
    for table in ("message", "chat", "handle", "chat_message_join", "attachment"):
        _line(table, "present" if schema.has_table(table) else "MISSING")
        if not schema.has_table(table):
            problems.append(f"required table `{table}` is missing")
    missing = schema.missing()
    if missing:
        for table, cols in sorted(missing.items()):
            _line(f"{table} (absent cols)", ", ".join(cols))
        print("  → substituted with NULL literals; the query still runs.")
    else:
        _line("Optional columns", "all present")

    _rule("Loading")
    start = datetime.now()
    corpus = load_corpus(con, schema)
    elapsed = (datetime.now() - start).total_seconds()
    s = corpus.stats
    _line("Joined rows scanned", f"{s.get('rows', 0):,}")
    _line("Real messages (rows)", f"{s.get('messages', 0):,}")
    _line("  distinct by ROWID", f"{s.get('distinct_messages', 0):,}")
    _line("Tapback rows", f"{s.get('tapbacks', 0):,}")
    _line("System events skipped", f"{s.get('skipped_system_event', 0):,}")
    _line(
        "Empty / no text skipped",
        f"{s.get('skipped_no_text', 0) + s.get('skipped_empty_flag', 0):,}",
    )
    _line("Elapsed", f"{elapsed:.1f}s")

    if not corpus.messages:
        problems.append("no messages loaded")

    _rule("Text resolution")
    candidates = s.get("candidates", 0)
    resolved = s.get("from_text", 0) + s.get("from_body", 0)
    rate = (resolved / candidates * 100) if candidates else 0.0
    _line("Candidate rows", f"{candidates:,}")
    _line("From message.text", f"{s.get('from_text', 0):,}")
    _line("attributedBody only", f"{s.get('body_only', 0):,}")
    _line("  decoded", f"{s.get('from_body', 0):,}")
    _line("  failed", f"{s.get('body_failed', 0):,}")
    _line("No text and no body", f"{s.get('no_content', 0):,}")
    _line("Resolution rate", f"{rate:.2f}%")
    if candidates and rate < TEXT_RESOLUTION_FLOOR:
        problems.append(
            f"text resolution {rate:.2f}% is below the {TEXT_RESOLUTION_FLOOR}% floor — "
            "stats built on this would be wrong"
        )

    _rule("Dates")
    if corpus.messages:
        stamps = [m.ts for m in corpus.messages]
        first, last = min(stamps), max(stamps)
        first_dt, last_dt = local_dt(first), local_dt(last)
        days = (last_dt.date() - first_dt.date()).days + 1
        _line("First message", first_dt.strftime("%Y-%m-%d %H:%M:%S"))
        _line("Last message", last_dt.strftime("%Y-%m-%d %H:%M:%S"))
        _line("Days covered", f"{days:,}")
        now = datetime.now()
        if first_dt.year < 2000:
            problems.append(f"first message dated {first_dt.year} — epoch conversion is wrong")
        if last_dt > now:
            problems.append(f"last message is in the future ({last_dt}) — check date units")
        raw_max = con.execute("SELECT MAX(date) AS d FROM message").fetchone()["d"]
        units = "nanoseconds" if raw_max and abs(raw_max) > NANOSECOND_THRESHOLD else "seconds"
        _line("Raw `date` units", units)

    _rule("Identity")
    _line("Distinct handles", f"{s.get('handles', 0):,}")
    _line("Contacts entries", f"{s.get('contacts_in_addressbook', 0):,}")
    _line("People", f"{s.get('people', 0):,}")
    _line("  named from Contacts", f"{s.get('people_named', 0):,}")
    if not corpus.contacts_resolved:
        print("  → Contacts unavailable; falling back to formatted phone numbers.")

    ranked = Counter()
    for m in corpus.messages:
        if m.chat_style == STYLE_DIRECT and m.handle:
            ranked[m.handle] += 1
    if ranked:
        print("\n  Top 1:1 handles (sanity check for name matching):")
        for handle, count in ranked.most_common(5):
            pid = corpus.handle_to_person.get(handle)
            person = corpus.people.get(pid) if pid else None
            label = person.name if person else format_handle(handle)
            print(f"    {label:<28} {count:,}")

    con.close()

    _rule("Result")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("  ✓ Phase 0 + 1 look correct.")
    return 0


def dump_stats(compact: bool = False, year: Optional[int] = None) -> int:
    """Print the whole report as JSON. Progress goes to stderr so stdout stays pure."""
    preflight()
    con = open_db()
    schema = Schema.detect(con)

    seen_steps: "set[str]" = set()

    def note(step: str, fraction: float = 0.0) -> None:
        if step not in seen_steps:  # one line per step, not one per progress tick
            seen_steps.add(step)
            print(f"  {step}…", file=sys.stderr)

    if year is not None:
        print(f"  Filtering to {year}…", file=sys.stderr)
    note("Reading messages")
    corpus = load_corpus(con, schema, year=year)
    report = build_report(corpus, con, schema, progress=note, year=year)
    con.close()

    # allow_nan=False turns any NaN/Infinity into an error instead of invalid JSON.
    json.dump(
        report,
        sys.stdout,
        indent=None if compact else 2,
        ensure_ascii=False,
        allow_nan=False,
    )
    print()
    return 0


# --------------------------------------------------------------------------------------
# Phase 3 — the local server
# --------------------------------------------------------------------------------------

PORT_FIRST = 8420
PORT_LAST = 8430
BROWSER_DELAY = 0.3

# The whole interface. Inlined because this app is one file and makes no network
# requests — no CDN, no font host, no chart library. Charts are hand-written SVG.
PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- The server sends this as a header too. Repeating it in the document means a saved
     copy, opened from the Finder with no server in front of it, still can't phone home. -->
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
      style-src 'unsafe-inline'; img-src data:; script-src 'unsafe-inline'; connect-src 'self'">
<title>iMessage Wrapped</title>
<style>
  :root {
    --imsg-blue:#0B84FE; --sms-green:#30D158; --bubble-grey:#E9E9EB;
    --ink:#0A0A0B; --paper:#FFFFFF; --muted:#8A8A8E; --hair:rgba(0,0,0,.10);
  }
  @media (prefers-color-scheme: dark) {
    :root { --bubble-grey:#26252A; --ink:#F5F5F7; --paper:#000000;
            --muted:#8A8A8E; --hair:rgba(255,255,255,.14); }
  }
  * { box-sizing:border-box; }
  html { -webkit-font-smoothing:antialiased; }
  body {
    margin:0; background:var(--paper); color:var(--ink);
    font:17px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue",
         Helvetica, Arial, sans-serif;
  }
  .wrap { width:min(680px, 92vw); margin:0 auto; }

  /* ---- landing ------------------------------------------------------------- */
  #landing { min-height:100vh; display:grid; place-items:center; text-align:center; }
  #landing h1 { font-size:clamp(34px,7vw,52px); font-weight:800; letter-spacing:-.03em;
                margin:0 0 10px; }
  #landing p { color:var(--muted); margin:0 auto 30px; max-width:34ch; font-size:15px; }
  button {
    font:inherit; font-size:16px; font-weight:600; color:#fff; background:var(--imsg-blue);
    border:0; border-radius:980px; padding:14px 32px; cursor:pointer;
    transition:transform .12s ease, opacity .2s ease;
  }
  button:hover:not(:disabled) { transform:scale(1.03); }
  button:active:not(:disabled) { transform:scale(.98); }
  button:disabled { opacity:.45; cursor:default; }
  /* Keyboard users get a ring; mouse users don't. Never remove it without replacing it. */
  :focus-visible { outline:2.5px solid var(--imsg-blue); outline-offset:3px;
                   border-radius:8px; }
  /* Year picker: on the landing page it chooses what to build, in the report it
     switches between the years already built. Same control, so the same styling. */
  .years { display:flex; flex-wrap:wrap; justify-content:center; gap:7px;
           width:min(430px,90vw); margin:0 auto 26px; }
  .years button { font-size:13px; padding:7px 15px; background:var(--bubble-grey);
                  color:var(--ink); }
  .years button.on { background:var(--imsg-blue); color:#fff; }
  .years button:hover:not(:disabled) { transform:none; opacity:.78; }
  .years:empty { display:none; }
  #bar { height:5px; border-radius:3px; background:var(--bubble-grey); overflow:hidden;
         margin:30px auto 12px; width:min(340px,80vw); display:none; }
  #fill { height:100%; width:0; background:var(--imsg-blue); }
  #step { color:var(--muted); font-size:13px; min-height:1.4em; }
  #err { display:none; text-align:left; white-space:pre-wrap; font-size:12px;
         line-height:1.5; background:var(--bubble-grey); border-radius:14px;
         padding:16px; margin-top:22px; max-height:52vh; overflow:auto; }

  /* ---- report -------------------------------------------------------------- */
  #report { display:none; padding:0 0 22vh; }
  .hero { padding:16vh 0 10vh; text-align:center; }
  .hero .eyebrow { font-size:11px; text-transform:uppercase; letter-spacing:.09em;
                   color:var(--muted); font-weight:600; }
  .hero h1 { font-size:clamp(38px,9vw,68px); font-weight:800; letter-spacing:-.035em;
             margin:14px 0 6px; }
  .hero .range { color:var(--muted); font-size:15px; }

  .divider { display:flex; align-items:center; gap:14px; margin:64px 0 26px;
             color:var(--muted); }
  .divider::before, .divider::after { content:""; flex:1; height:1px; background:var(--hair); }
  .divider span { font-size:11px; text-transform:uppercase; letter-spacing:.09em;
                  font-weight:600; white-space:nowrap; }

  /* Chat thread. Bubble geometry is copied from Messages, tails included. */
  .thread { display:flex; flex-direction:column; gap:3px; }
  .row { display:flex; }
  .row.me { justify-content:flex-end; }
  .row + .row.turn { margin-top:9px; }
  .b {
    position:relative; max-width:76%; padding:9px 15px 10px; border-radius:19px;
    font-size:16.5px; line-height:1.36; word-break:break-word;
    opacity:0; transform:translateY(14px) scale(.94); transform-origin:bottom;
  }
  .b.in { animation:pop .42s cubic-bezier(.2,.9,.3,1.15) forwards; }
  @keyframes pop { to { opacity:1; transform:none; } }
  .b.them { background:var(--bubble-grey); color:var(--ink); transform-origin:bottom left; }
  .b.me   { background:var(--imsg-blue); color:#fff; transform-origin:bottom right; }
  .b.tail.them { border-bottom-left-radius:5px; }
  .b.tail.me   { border-bottom-right-radius:5px; }
  .b.tail.them::before, .b.tail.me::before {
    content:""; position:absolute; bottom:0; width:19px; height:19px;
  }
  .b.tail.them::before { left:-7px; background:var(--bubble-grey);
                         border-bottom-right-radius:16px 14px; }
  .b.tail.me::before   { right:-7px; background:var(--imsg-blue);
                         border-bottom-left-radius:16px 14px; }
  .b.tail.them::after, .b.tail.me::after {
    content:""; position:absolute; bottom:0; width:11px; height:19px; background:var(--paper);
  }
  .b.tail.them::after { left:-11px; border-bottom-right-radius:11px; }
  .b.tail.me::after   { right:-11px; border-bottom-left-radius:11px; }
  .b .big { display:block; font-size:33px; font-weight:800; letter-spacing:-.02em;
            font-variant-numeric:tabular-nums; line-height:1.1; margin:2px 0 3px; }
  .b .sub { display:block; font-size:13px; opacity:.66; margin-top:3px; }
  .stamp { text-align:center; color:var(--muted); font-size:11px; margin:8px 0 2px;
           font-weight:600; letter-spacing:.02em; }
  .typing { display:inline-flex; gap:4px; padding:14px 16px; }
  .typing i { width:8px; height:8px; border-radius:50%; background:var(--muted);
              animation:blink 1.2s infinite; }
  .typing i:nth-child(2) { animation-delay:.18s; }
  .typing i:nth-child(3) { animation-delay:.36s; }
  @keyframes blink { 0%,60%,100% { opacity:.28; } 30% { opacity:.9; } }

  /* Nothing below the hero is visible on load, so say so — quietly, pinned to the
     bottom edge, gone the moment you scroll. */
  .cue { position:fixed; left:0; right:0; bottom:13px; z-index:5; pointer-events:none;
         display:flex; align-items:center; justify-content:center; gap:5px;
         color:var(--muted); font-size:10px; text-transform:uppercase;
         letter-spacing:.11em; font-weight:600; transition:opacity .4s ease; }
  .cue.gone { opacity:0; }
  .cue svg { animation:nudge 1.9s ease-in-out infinite; }
  @keyframes nudge { 0%,100% { transform:translateY(0); } 50% { transform:translateY(4px); } }

  /* ---- ranked list (people, groups) --------------------------------------- */
  .rank { display:flex; flex-direction:column; gap:2px; }
  .rank .r {
    display:grid; grid-template-columns:22px 1fr auto; align-items:center; gap:12px;
    padding:11px 13px; border-radius:13px; position:relative; overflow:hidden;
    background:none; border:0; font:inherit; color:inherit; text-align:left; width:100%;
    cursor:pointer; transition:background .15s ease;
  }
  .rank .r[disabled] { cursor:default; }
  .rank .r:hover:not([disabled]) { background:var(--bubble-grey); }
  .rank .r .i { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
  .rank .r .nm { font-weight:600; overflow:hidden; text-overflow:ellipsis;
                 white-space:nowrap; }
  .rank .r .ct { font-variant-numeric:tabular-nums; color:var(--muted); font-size:14px; }
  .rank .r .track { grid-column:2 / 4; height:5px; border-radius:3px;
                    background:var(--bubble-grey); overflow:hidden; margin-top:6px; }
  .rank .r .track i { display:block; height:100%; width:0; border-radius:3px;
                      background:var(--imsg-blue); transition:width .9s cubic-bezier(.2,.8,.2,1); }
  .rank .r .track i.g { background:var(--sms-green); }

  /* ---- charts -------------------------------------------------------------- */
  .card { background:var(--bubble-grey); border-radius:20px; padding:20px 20px 14px; }
  .card h3 { margin:0 0 2px; font-size:15px; font-weight:700; }
  .card .cap { margin:0 0 16px; color:var(--muted); font-size:13px; }
  svg { display:block; width:100%; height:auto; overflow:visible; }
  svg .bar { fill:var(--imsg-blue); transition:height .8s cubic-bezier(.2,.8,.2,1),
                                                y .8s cubic-bezier(.2,.8,.2,1); }
  svg .bar.dim { fill:var(--muted); opacity:.34; }
  svg .lbl { fill:var(--muted); font-size:10px; font-weight:600; letter-spacing:.04em; }
  svg .area { fill:var(--imsg-blue); opacity:.16; }
  svg .line { fill:none; stroke:var(--imsg-blue); stroke-width:2.5;
              stroke-linejoin:round; stroke-linecap:round; }
  svg .dot { fill:var(--imsg-blue); }

  /* ---- emoji + chips -------------------------------------------------------- */
  .emoji-row { display:flex; gap:10px; flex-wrap:wrap; }
  .emoji-row .e { flex:1 1 78px; text-align:center; background:var(--bubble-grey);
                  border-radius:16px; padding:14px 6px 11px; }
  .emoji-row .e .g { font-size:31px; line-height:1.15;
                     font-family:"Apple Color Emoji", sans-serif; }
  .emoji-row .e .c { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums;
                     margin-top:5px; }
  .stack { display:grid; gap:14px; }

  /* ---- contact search -------------------------------------------------------- */
  .find { -webkit-appearance:none; appearance:none; font:inherit; font-size:15px;
          width:100%; padding:11px 15px; border-radius:13px; border:1px solid var(--hair);
          background:var(--paper); color:var(--ink); }
  .find::placeholder { color:var(--muted); }
  .hits:not(:empty) { margin-top:10px; }
  .hits .r:hover:not([disabled]) { background:var(--paper); }
  .hits .cap { margin:12px 0 2px; }

  .chips { display:flex; flex-wrap:wrap; gap:8px; }
  .chip { background:var(--bubble-grey); border-radius:980px; padding:7px 14px; font-size:14px; }
  .chip b { font-variant-numeric:tabular-nums; }

  /* ---- person drill-down ---------------------------------------------------- */
  #detail { position:fixed; inset:0; background:var(--paper); overflow-y:auto;
            z-index:20; display:none; }
  #detail.open { display:block; animation:slideup .34s cubic-bezier(.2,.9,.3,1) both; }
  @keyframes slideup { from { transform:translateY(22px); opacity:0; } }
  .backbar { position:sticky; top:0; background:var(--paper); padding:14px 0 12px;
             border-bottom:1px solid var(--hair); z-index:2; }
  .back { background:none; border:0; color:var(--imsg-blue); font:inherit; font-size:16px;
          font-weight:500; cursor:pointer; padding:0; display:inline-flex;
          align-items:center; gap:5px; }
  .back:hover { transform:none; opacity:.7; }

  footer { text-align:center; margin-top:72px; padding-top:30px;
           border-top:1px solid var(--hair); color:var(--muted); font-size:13px; }
  footer button { background:var(--bubble-grey); color:var(--ink); font-size:14px;
                  padding:11px 22px; }
  .saves { display:flex; flex-wrap:wrap; gap:10px; justify-content:center;
           margin-bottom:16px; }

  @media (prefers-reduced-motion: reduce) {
    .b { opacity:1; transform:none; }
    .b.in, .cue svg, #detail.open { animation:none; }
    .typing i { animation:none; }
    .rank .r .track i, svg .bar { transition:none; }
  }
</style></head>
<body>

<div id="landing"><div class="wrap">
  <h1>iMessage Wrapped</h1>
  <p>Built on this Mac, from your own Messages history.
     Nothing is uploaded, and nothing leaves this machine.</p>
  <div class="years" id="yearPick" role="group" aria-label="Which year to build"></div>
  <button id="go">Generate my Wrapped</button>
  <div id="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div id="fill"></div></div>
  <div id="step" aria-live="polite"></div>
  <pre id="err" role="alert"></pre>
</div></div>

<div id="report"><div class="wrap">
  <div class="hero">
    <div class="eyebrow">Your Messages, wrapped</div>
    <h1 id="heroCount">0</h1>
    <div class="range" id="heroRange"></div>
  </div>
  <div class="years" id="yearSwitch" role="group" aria-label="Filter this report by year"></div>
  <div id="sections"></div>
  <footer>
    <div class="saves">
      <button id="saveHtml">Save a copy</button>
      <button id="export">Download the raw JSON</button>
    </div>
    <div id="foot">Everything here was computed on this Mac. Nothing was uploaded.</div>
  </footer>
</div>
<div class="cue" id="cue">Scroll
  <svg width="13" height="8" viewBox="0 0 17 11" aria-hidden="true">
    <path d="M1.5 1.5 L8.5 8.5 L15.5 1.5" fill="none" stroke="currentColor"
          stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
</div></div>

<div id="detail"><div class="wrap">
  <div class="backbar"><button class="back" id="back">‹ Back</button></div>
  <div id="detailBody"></div>
</div></div>

<script>
'use strict';
/* "Save a copy" bakes the finished report in on the line below, so the saved file
   opens from the Finder years later with no Python, no server and no network. In the
   live app it stays null and the report arrives over HTTP instead. */
const EMBEDDED = null;

const $ = (id) => document.getElementById(id);
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
let DATA = null;

/* ---- formatting --------------------------------------------------------- */
const nf = new Intl.NumberFormat();
const num = (n) => nf.format(Math.round(n));

function dur(s) {
  if (s == null) return '—';
  if (s < 60) return (s < 10 ? s.toFixed(1) : Math.round(s)) + ' sec';
  if (s < 3600) return Math.round(s / 60) + ' min';
  if (s < 86400) return +(s / 3600).toFixed(1) + ' hr';
  return num(s / 86400) + ' days';
}

function monthYear(iso) {
  return new Date(iso).toLocaleDateString(undefined, { month:'long', year:'numeric' });
}

/* Dates arrive as ISO strings; parse the parts by hand so a bare "2026-06-20" isn't
   read as UTC midnight and shown as the day before. */
function shortDate(iso, withYear) {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined,
    withYear ? { month:'short', day:'numeric', year:'numeric' }
             : { month:'short', day:'numeric' });
}

function dateRange(a, b) {
  return shortDate(a, a.slice(0, 4) !== b.slice(0, 4)) + ' – ' + shortDate(b, true);
}

function hour12(h) {
  const period = h < 12 ? 'AM' : 'PM';
  return ((h % 12) || 12) + ' ' + period;
}

function firstName(name) {
  return /^[+(\\d]/.test(name) ? name : name.split(' ')[0];
}

/* ---- thread building ----------------------------------------------------- */
/* Each bubble is a statistic. `who` is 'me' (blue, right) or 'them' (grey, left);
   a tail is drawn on the last bubble of a run, exactly like Messages does it. */
function thread(bubbles) {
  const el = document.createElement('div');
  el.className = 'thread';
  bubbles.forEach((b, i) => {
    if (b.stamp) {
      const s = document.createElement('div');
      s.className = 'stamp'; s.textContent = b.stamp; el.appendChild(s);
      return;
    }
    const row = document.createElement('div');
    const last = i === bubbles.length - 1 || bubbles[i + 1].who !== b.who
                 || bubbles[i + 1].stamp;
    const prev = i > 0 && !bubbles[i - 1].stamp ? bubbles[i - 1].who : null;
    row.className = 'row ' + b.who + (prev && prev !== b.who ? ' turn' : '');
    const bub = document.createElement('div');
    bub.className = 'b ' + b.who + (last ? ' tail' : '');
    /* `big` counts up from zero; `raw` is already-formatted text ("28 sec") that
       has nothing to animate. Either one renders as the headline number. */
    if (b.big != null || b.raw != null) {
      const big = document.createElement('span');
      big.className = 'big';
      if (b.big != null) {
        big.dataset.to = b.big;
        big.dataset.suffix = b.suffix || '';
        big.textContent = '0';
      } else {
        big.textContent = b.raw;
      }
      bub.appendChild(big);
    }
    const txt = document.createElement('span');
    txt.textContent = b.text;
    bub.appendChild(txt);
    if (b.sub) {
      const sub = document.createElement('span');
      sub.className = 'sub'; sub.textContent = b.sub; bub.appendChild(sub);
    }
    row.appendChild(bub);
    el.appendChild(row);
  });
  return el;
}

function divider(label) {
  const d = document.createElement('div');
  d.className = 'divider';
  d.innerHTML = '<span></span>';
  d.firstChild.textContent = label;
  return d;
}

/* Bubbles arrive one at a time, the way a real thread fills in. */
function reveal(root) {
  const bubbles = [...root.querySelectorAll('.b')];
  /* Bubbles start at opacity 0, so if the observer can't run they'd stay invisible.
     Showing everything is the safe failure. */
  if (!('IntersectionObserver' in window)) {
    bubbles.forEach((b) => { b.classList.add('in'); countUp(b); });
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (!e.isIntersecting) return;
      io.unobserve(e.target);
      const group = [...e.target.closest('.thread').querySelectorAll('.b')];
      const i = group.indexOf(e.target);
      setTimeout(() => { e.target.classList.add('in'); countUp(e.target); },
                 REDUCED ? 0 : Math.min(i, 6) * 120);
    });
  }, { threshold:0.2, rootMargin:'0px 0px -8% 0px' });
  bubbles.forEach((b) => io.observe(b));
}

function countUp(bubble) {
  const el = bubble.querySelector('.big[data-to]');
  if (!el) return;
  const to = parseFloat(el.dataset.to), suffix = el.dataset.suffix;
  if (REDUCED || !isFinite(to)) { el.textContent = num(to) + suffix; return; }
  const start = performance.now(), ms = 1000;
  (function frame(t) {
    const p = Math.min(1, (t - start) / ms);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = num(to * eased) + suffix;
    if (p < 1) requestAnimationFrame(frame);
  })(start);
}

/* ---- the headline: your top conversation, rendered as a conversation ------ */
function headline(d) {
  const top = (d.global.top_people || []).find((p) => p.two_way)
              || (d.global.top_people || [])[0];
  if (!top) return null;
  const person = d.people.find((p) => p.person_id === top.person_id);
  if (!person) return null;
  const s = person.stats, name = firstName(person.name);

  const bubbles = [
    { stamp: monthYear(s.totals.first_message) },
    { who:'them', big: s.totals.total, text: 'messages between you and ' + person.name + '.',
      sub: 'Across ' + num(s.totals.active_days) + ' days you actually talked.' },
    { who:'me', big: s.totals.sent, text: 'of them were yours.',
      sub: Math.round(s.totals.sent / s.totals.total * 100) + '% of the thread' },
  ];

  if (s.reply.you_replies > 20) {
    bubbles.push({ who:'them', raw: dur(s.reply.them_median_seconds),
                   text: 'is how long ' + name + ' makes you wait, typically.' });
    bubbles.push({ who:'me', raw: dur(s.reply.you_median_seconds),
                   text: 'is how long you make them wait.',
                   sub: s.reply.you_median_seconds <= s.reply.them_median_seconds
                        ? 'You are the faster one.' : name + ' is the faster one.' });
  }
  if (s.streak.days > 1) {
    bubbles.push({ who:'them', big: s.streak.days, text: 'days in a row, your longest run.',
                   sub: dateRange(s.streak.start, s.streak.end) });
  }
  bubbles.push({ who:'me', raw: hour12(s.hours.peak_hour),
                 text: 'on a ' + s.days.busiest_day_of_week + ' is when you two talk most.' });

  const wrap = document.createElement('section');
  wrap.appendChild(divider('Your top conversation'));
  wrap.appendChild(thread(bubbles));
  return wrap;
}

/* ---- small builders ------------------------------------------------------ */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function section(label, ...nodes) {
  const s = document.createElement('section');
  s.appendChild(divider(label));
  nodes.forEach((n) => n && s.appendChild(n));
  return s;
}

function card(title, caption, body) {
  const c = el('div', 'card');
  if (title) c.appendChild(el('h3', null, title));
  if (caption) c.appendChild(el('p', 'cap', caption));
  c.appendChild(body);
  return c;
}

function chips(pairs) {
  const c = el('div', 'chips');
  pairs.forEach(([label, value]) => {
    const chip = el('div', 'chip');
    chip.appendChild(document.createTextNode(label + ' '));
    const b = el('b', null, value);
    chip.appendChild(b);
    c.appendChild(chip);
  });
  return c;
}

/* SVG is built through the namespaced API rather than innerHTML: report values go in
   as text nodes and attributes, so a contact named like markup can't inject anything. */
const SVGNS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

/* ---- ranked list --------------------------------------------------------- */
function rankList(items, onClick) {
  const list = el('div', 'rank');
  const max = Math.max(...items.map((i) => i.value), 1);
  items.forEach((item, i) => {
    const row = el('button', 'r');
    row.type = 'button';
    if (!onClick) row.disabled = true;
    row.appendChild(el('span', 'i', String(i + 1)));
    row.appendChild(el('span', 'nm', item.name));
    row.appendChild(el('span', 'ct', num(item.value)));
    const track = el('div', 'track');
    const fill = el('i', item.green ? 'g' : null);
    track.appendChild(fill);
    row.appendChild(track);
    // Widths are set after layout so the bars animate out from zero.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      fill.style.width = (item.value / max * 100).toFixed(1) + '%';
    }));
    if (onClick) row.onclick = () => onClick(item);
    list.appendChild(row);
  });
  return list;
}

/* ---- charts -------------------------------------------------------------- */
/* role="img" plus a <title> child is what a screen reader reads out; without it a
   chart is announced as an unlabelled graphic, which is worse than nothing. */
function chartRoot(viewBox, title) {
  const svg = svgEl('svg', { viewBox, role: 'img' });
  const t = svgEl('title');
  t.textContent = title;
  svg.appendChild(t);
  return svg;
}

function barChart(values, labelAt, highlight, title) {
  const W = 620, H = 150, n = values.length;
  const gap = n > 12 ? 3 : 8, bw = (W - gap * (n - 1)) / n;
  const max = Math.max(...values, 1);
  const svg = chartRoot(`0 0 ${W} ${H + 20}`, title || 'Bar chart');
  values.forEach((v, i) => {
    const h = Math.max(2, v / max * H);
    const x = i * (bw + gap);
    const bar = svgEl('rect', {
      x, y: H, width: bw, height: 0, rx: Math.min(4, bw / 2),
      class: 'bar' + (highlight != null && i !== highlight ? ' dim' : ''),
    });
    svg.appendChild(bar);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      bar.setAttribute('y', H - h); bar.setAttribute('height', h);
    }));
    const label = labelAt(i);
    if (label) {
      const t = svgEl('text', { x: x + bw / 2, y: H + 15, class: 'lbl',
                                'text-anchor': 'middle' });
      t.textContent = label;
      svg.appendChild(t);
    }
  });
  return svg;
}

function areaChart(points, labelAt, title) {
  const W = 620, H = 160, n = points.length;
  const max = Math.max(...points.map((p) => p.count), 1);
  const x = (i) => (n === 1 ? W / 2 : i / (n - 1) * W);
  const y = (v) => H - (v / max) * (H - 12);
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.count).toFixed(1)}`);
  const svg = chartRoot(`0 0 ${W} ${H + 20}`, title || 'Line chart');
  svg.appendChild(svgEl('path', {
    class: 'area', d: line.join(' ') + ` L${W} ${H} L0 ${H} Z` }));
  svg.appendChild(svgEl('path', { class: 'line', d: line.join(' ') }));
  const peak = points.reduce((a, p, i) => (p.count > points[a].count ? i : a), 0);
  svg.appendChild(svgEl('circle', { class: 'dot', cx: x(peak), cy: y(points[peak].count), r: 4 }));
  points.forEach((p, i) => {
    const label = labelAt(i, p);
    if (!label) return;
    const t = svgEl('text', { x: x(i), y: H + 16, class: 'lbl', 'text-anchor':
      i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle' });
    t.textContent = label;
    svg.appendChild(t);
  });
  return svg;
}

/* ---- sections ------------------------------------------------------------ */
function peopleSection(d, onPerson) {
  const top = (d.global.top_people || []).filter((p) => p.total > 0);
  if (top.length < 2) return null;
  const wrap = el('div', 'stack');
  wrap.appendChild(
    card('Who you actually talk to', 'Every message either way. Tap a name for their own page.',
      rankList(top.map((p) => ({ name: p.name, value: p.total, id: p.person_id })), onPerson)));
  const search = peopleSearch(d, onPerson);
  if (search) wrap.appendChild(search);
  return section('The people', wrap);
}

/* The top ten are the story; everyone else needs a way in. Filtering happens over the
   already-loaded report, so typing never touches the disk or the network. */
const SEARCH_LIMIT = 8;
function peopleSearch(d, onPerson) {
  const all = (d.people || []).filter((p) => p.total > 0);
  if (all.length <= 10) return null;

  const input = document.createElement('input');
  input.type = 'search';
  input.className = 'find';
  input.placeholder = 'Search all ' + num(all.length) + ' people';
  input.setAttribute('aria-label', 'Search everyone you have messaged');

  const results = el('div', 'hits');
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    results.textContent = '';
    if (!q) return;
    const hits = all.filter((p) => p.name.toLowerCase().includes(q)).slice(0, SEARCH_LIMIT);
    if (!hits.length) {
      results.appendChild(el('p', 'cap', 'Nobody by that name.'));
      return;
    }
    results.appendChild(rankList(
      hits.map((p) => ({ name: p.name, value: p.total, id: p.person_id })), onPerson));
  };
  input.oninput = draw;
  // Enter opens the best match, so the whole thing works without ever leaving the keyboard.
  input.onkeydown = (e) => {
    if (e.key !== 'Enter') return;
    const q = input.value.trim().toLowerCase();
    const hit = all.find((p) => p.name.toLowerCase().includes(q));
    if (q && hit) onPerson({ id: hit.person_id });
  };

  const box = el('div', 'find-box');
  box.appendChild(input);
  box.appendChild(results);
  return card('Look someone up', 'Anyone you have ever messaged, by name or number', box);
}

function timeSection(s, label) {
  const hours = s.hours.histogram.map((h) => h.count);
  const dows = s.days.day_of_week;
  const peakDow = dows.reduce((a, x, i) => (x.count > dows[a].count ? i : a), 0);
  const hoursCard = card('By hour of day',
    'You peak at ' + hour12(s.hours.peak_hour) + ' · ' + num(s.hours.peak_count) + ' messages',
    barChart(hours, (i) => (i % 6 === 0 ? hour12(i) : ''), s.hours.peak_hour,
      'Messages by hour of day, peaking at ' + hour12(s.hours.peak_hour) +
      ' with ' + num(s.hours.peak_count)));
  const dowCard = card('By day of week', s.days.busiest_day_of_week + ' is your busiest',
    barChart(dows.map((x) => x.count), (i) => dows[i].day.slice(0, 3), peakDow,
      'Messages by day of week, busiest on ' + s.days.busiest_day_of_week));
  const wrap = el('div');
  wrap.style.display = 'grid';
  wrap.style.gap = '14px';
  wrap.appendChild(hoursCard);
  wrap.appendChild(dowCard);
  return section(label || 'When you text', wrap);
}

function volumeSection(s, label) {
  const m = s.volume.monthly;
  if (!m || m.length < 3) return null;
  const peak = m.reduce((a, p) => (p.count > a.count ? p : a), m[0]);
  const monthLabel = (key) => {
    const [y, mo] = key.split('-').map(Number);
    return new Date(y, mo - 1, 1).toLocaleDateString(undefined,
      { month: 'short', year: '2-digit' });
  };
  return section(label || 'Month by month',
    card('Messages per month',
      'Busiest was ' + monthLabel(peak.month) + ' with ' + num(peak.count),
      areaChart(m, (i, p) =>
        (i === 0 || i === m.length - 1 || p.month === peak.month ? monthLabel(p.month) : ''),
        'Messages per month from ' + monthLabel(m[0].month) + ' to ' +
        monthLabel(m[m.length - 1].month) + ', peaking at ' + num(peak.count) +
        ' in ' + monthLabel(peak.month))));
}

function emojiSection(s) {
  const e = s.emoji;
  const top = e.combined_by_message || [];
  if (!top.length) return null;
  const row = el('div', 'emoji-row');
  top.slice(0, 8).forEach(([glyph, count]) => {
    const box = el('div', 'e');
    box.appendChild(el('div', 'g', glyph));
    box.appendChild(el('div', 'c', num(count)));
    row.appendChild(box);
  });
  const yours = (e.you_by_message || [])[0];
  const theirs = (e.them_by_message || [])[0];
  const extra = [];
  if (yours) extra.push(['Yours is', yours[0] + ' ' + num(yours[1])]);
  if (theirs) extra.push(['Theirs is', theirs[0] + ' ' + num(theirs[1])]);
  extra.push(['Messages with emoji',
    num(e.you_messages_with_emoji + e.them_messages_with_emoji)]);
  const wrap = el('div');
  wrap.style.display = 'grid';
  wrap.style.gap = '14px';
  wrap.appendChild(row);
  wrap.appendChild(chips(extra));
  // Ranked by how many messages each emoji appeared in, not raw occurrences — one
  // spam message pasting the same emoji 7,000 times would otherwise win outright.
  return section('Emoji', wrap);
}

function reactionSection(s) {
  const t = s.tapbacks;
  if (!t || (t.given_total + t.received_total) === 0) return null;
  const order = ['Loved', 'Liked', 'Laughed', 'Emphasized', 'Questioned', 'Disliked',
                 'Emoji reactions'];
  const rows = order
    .map((k) => ({ name: k, value: (t.received[k] || 0) + (t.given[k] || 0) }))
    .filter((r) => r.value > 0)
    .sort((a, b) => b.value - a.value);
  const wrap = el('div');
  wrap.style.display = 'grid';
  wrap.style.gap = '14px';
  wrap.appendChild(card('Tapbacks',
    num(t.received_total) + ' received · ' + num(t.given_total) + ' given',
    rankList(rows, null)));
  if (t.top_reactors && t.top_reactors.length) {
    wrap.appendChild(chips(t.top_reactors.slice(0, 4)
      .map((r) => [r.name, num(r.count)])));
  }
  return section('Reactions', wrap);
}

function attachmentSection(s) {
  const b = s.attachments.buckets, total = s.attachments.total;
  if (!total) return null;
  const rows = Object.keys(b).map((k) => ({ name: k, value: b[k], green: true }))
    .filter((r) => r.value > 0).sort((x, y) => y.value - x.value);
  if (!rows.length) return null;
  const wrap = el('div');
  wrap.style.display = 'grid';
  wrap.style.gap = '14px';
  wrap.appendChild(card('What you send', num(total) + ' attachments in total',
    rankList(rows, null)));
  if (s.attachments.voice_notes) {
    wrap.appendChild(chips([['Voice notes', num(s.attachments.voice_notes)]]));
  }
  return section('Photos and files', wrap);
}

/* Habits close the report the way it opened — as a thread. */
function habitsSection(s) {
  const bubbles = [];
  if (s.streak.days > 1) {
    bubbles.push({ who: 'them', big: s.streak.days, text: 'days in a row without a gap.',
                   sub: dateRange(s.streak.start, s.streak.end) });
  }
  if (s.conversations.total > 0) {
    const share = Math.round(s.conversations.you_started / s.conversations.total * 100);
    bubbles.push({ who: 'me', big: s.conversations.you_started,
                   text: 'conversations you started first.',
                   sub: share + '% of them — the other ' +
                        num(s.conversations.they_started) + ' came to you.' });
  }
  if (s.late_night.count > 0) {
    bubbles.push({ who: 'them', big: s.late_night.count,
                   text: 'messages after midnight.',
                   sub: s.late_night.share + '% of everything you send and receive.' });
    if (s.late_night.most_nocturnal) {
      const n = s.late_night.most_nocturnal;
      bubbles.push({ who: 'me', raw: n.share + '%',
                     text: 'of what ' + firstName(n.name) + ' sends you lands after midnight.',
                     sub: 'Your most nocturnal contact.' });
    }
  }
  if (s.days.busiest_date) {
    bubbles.push({ who: 'them', big: s.days.busiest_date.count,
                   text: 'messages in a single day, your record.',
                   sub: shortDate(s.days.busiest_date.date, true) });
  }
  if (s.length && s.length.longest) {
    bubbles.push({ who: 'me', big: s.length.longest.chars, text: 'characters in one message.',
                   sub: (s.length.longest.from_me ? 'You wrote it, ' : 'Someone sent it to you, ') +
                        shortDate(s.length.longest.date, true) });
  }
  if (!bubbles.length) return null;
  return section('Your habits', thread(bubbles));
}

/* Calls come from a separate database that plenty of Macs don't have. When it's
   missing the report just doesn't mention calls. */
function callSection(c) {
  if (!c || !c.total) return null;
  const bubbles = [
    { who: 'them', big: c.total, text: 'calls, on the phone and on FaceTime.',
      sub: num(c.outgoing) + ' out · ' + num(c.incoming) + ' in' +
           (c.missed ? ' · ' + num(c.missed) + ' missed' : '') },
    { who: 'me', raw: dur(c.total_seconds), text: 'spent talking.',
      sub: 'Averaging ' + dur(c.mean_seconds) + ' a call.' },
  ];
  if (c.longest && c.longest.seconds > 0) {
    bubbles.push({ who: 'them', raw: dur(c.longest.seconds),
                   text: 'was your longest single call.',
                   sub: (c.longest.name ? 'With ' + firstName(c.longest.name) + ', ' : '') +
                        shortDate(c.longest.date, true) });
  }
  if (c.peak_hour != null) {
    bubbles.push({ who: 'me', raw: hour12(c.peak_hour), text: 'is when you usually pick up.' });
  }
  const s = section('Calls', thread(bubbles));
  const top = (c.top_people || []).filter((p) => p[0]);
  if (top.length > 1) {
    s.appendChild(card('Who you actually call', 'By number of calls',
      rankList(top.map((p) => ({ name: p[0], value: p[1] })), null)));
  }
  return s;
}

/* Under a hundred messages there is nothing to be dramatic about, and a page of
   empty charts reads as broken. Say what's there and why, instead. */
const MIN_MESSAGES = 100;
function thinSection(d) {
  const t = d.global.totals;
  const why = YEAR == null
    ? 'That may mean Messages on this Mac has only recently started syncing your history — ' +
      'iCloud keeps older conversations on the server until you open them.'
    : 'Try All time, or another year — this Mac may not hold your whole history for ' +
      YEAR + '.';
  const lines = [
    { who: 'them', big: t.total, text: t.total === 1 ? 'message in this Mac’s library.'
                                                     : 'messages in this Mac’s library.' },
    { who: 'me', text: 'That’s too few to make a Wrapped out of — the charts would all be ' +
                       'one bar high.' },
    { who: 'them', text: why },
  ];
  return section('Not much to work with', thread(lines));
}

/* A group with no display name falls back to its chat identifier, which is a 20-digit
   number nobody recognises. Say who's in it instead. */
function groupName(g) {
  if (g.name && !/^chat\d+$/.test(g.name)) return g.name;
  return g.members > 1 ? 'Unnamed group · ' + g.members + ' people' : 'Unnamed group';
}

function groupSection(d) {
  const all = (d.groups || []).filter((x) => x.total > 0);
  if (!all.length) return null;
  const top = all.slice(0, 10);
  return section('Group chats',
    card('Where the noise is',
      num(all.length) + ' group' + (all.length === 1 ? '' : 's') +
      ', busiest first',
      rankList(top.map((x) => ({ name: groupName(x), value: x.total, green: true })), null)));
}

/* ---- person drill-down ---------------------------------------------------- */
function openPerson(item) {
  const person = DATA.people.find((p) => p.person_id === item.id);
  if (!person) return;
  const s = person.stats, name = firstName(person.name);
  const body = $('detailBody');
  body.textContent = '';

  const hero = el('div', 'hero');
  hero.appendChild(el('div', 'eyebrow', 'Your thread with'));
  hero.appendChild(el('h1', null, person.name));
  hero.appendChild(el('div', 'range',
    monthYear(s.totals.first_message) + ' – ' + monthYear(s.totals.last_message) +
    ' · ' + num(s.totals.active_days) + ' active days'));
  body.appendChild(hero);

  const bubbles = [
    { who: 'them', big: s.totals.received, text: 'messages from ' + name + '.' },
    { who: 'me', big: s.totals.sent, text: 'messages from you.',
      sub: Math.round(s.totals.sent / s.totals.total * 100) + '% of the thread' },
  ];
  if (s.reply.you_replies > 10) {
    bubbles.push({ who: 'them', raw: dur(s.reply.them_median_seconds),
                   text: 'median reply from ' + name + '.' });
    bubbles.push({ who: 'me', raw: dur(s.reply.you_median_seconds),
                   text: 'median reply from you.' });
  }
  if (s.length) {
    bubbles.push({ who: 'them', big: s.length.you_median,
                   text: 'characters in your typical message.',
                   sub: name + ' averages ' + num(s.length.them_median) + '.' });
  }
  body.appendChild(section('The numbers', thread(bubbles)));

  const parts = [timeSection(s, 'When you two talk'), volumeSection(s, 'Over time'),
                 emojiSection(s), reactionSection(s), attachmentSection(s),
                 habitsSection(s)];
  parts.forEach((p) => p && body.appendChild(p));

  $('detail').classList.add('open');
  $('detail').scrollTop = 0;
  document.body.style.overflow = 'hidden';
  history.pushState({ person: item.id }, '');
  reveal(body);
}

function closePerson() {
  $('detail').classList.remove('open');
  document.body.style.overflow = '';
}

$('back').onclick = () => history.state && history.state.person ? history.back() : closePerson();
window.onpopstate = closePerson;

/* ---- year filter ---------------------------------------------------------- */
/* Pick a year before generating, or switch afterwards. Years you have already built
   are cached server-side, so going back to one is instant; a new year is a fresh pass
   and shows the progress bar again. */
let YEAR = null;          // null means all of time
let YEARS = [];           // every calendar year the library covers, newest first
const READY = [];         // year keys already built this session

const yKey = (y) => (y == null ? 'all' : String(y));
const yLabel = (y) => (y == null ? 'All time' : String(y));
const yQuery = (y) => (y == null ? '' : '?year=' + y);

function yearChips(host, current, onPick, busy) {
  host.textContent = '';
  if (!YEARS.length) return;
  [null].concat(YEARS).forEach((y) => {
    const b = document.createElement('button');
    b.textContent = yKey(y) === busy ? yLabel(y) + '…' : yLabel(y);
    if (yKey(y) === yKey(current)) b.className = 'on';
    b.disabled = busy != null;
    b.onclick = () => onPick(y);
    host.appendChild(b);
  });
}

const drawPicker = () => yearChips($('yearPick'), YEAR, (y) => { YEAR = y; drawPicker(); }, null);
const drawSwitch = (busy) => yearChips($('yearSwitch'), YEAR, switchYear, busy);

async function switchYear(y) {
  if (yKey(y) === yKey(YEAR)) return;
  if (READY.indexOf(yKey(y)) >= 0) {
    YEAR = y;
    render(await (await fetch('/api/report' + yQuery(y))).json());
    return;
  }
  drawSwitch(yKey(y));
  await generate(y);
}

/* Asked for before anything has been generated. If the database can't be read yet —
   no Full Disk Access — this comes back empty and the picker simply doesn't appear. */
async function loadYears() {
  try {
    YEARS = (await (await fetch('/api/years')).json()).years || [];
  } catch (e) { YEARS = []; }
  drawPicker();
}

/* ---- render -------------------------------------------------------------- */
function render(d) {
  DATA = d;
  YEAR = d.meta.year == null ? null : d.meta.year;
  // A saved copy holds one report and has no server to ask for another, so it gets
  // no year switcher — offering years it can't build would be a dead control.
  if (!EMBEDDED && d.meta.years_available && d.meta.years_available.length) {
    YEARS = d.meta.years_available;
  }
  if (READY.indexOf(yKey(YEAR)) < 0) READY.push(yKey(YEAR));

  const t = d.global.totals;
  $('heroRange').textContent = t.total
    ? monthYear(t.first_message) + ' – ' + monthYear(t.last_message) +
      ' · ' + num(t.days_covered) + ' days'
    : (YEAR == null ? 'No messages found' : 'No messages in ' + YEAR);
  $('landing').style.display = 'none';
  $('report').style.display = 'block';
  $('go').disabled = false;
  $('bar').style.display = 'none';
  drawSwitch(null);

  countUpEl($('heroCount'), t.total, ' messages');

  const sections = $('sections');
  sections.textContent = '';
  const parts = t.total < MIN_MESSAGES ? [thinSection(d)] : [
    headline(d),
    peopleSection(d, openPerson),
    timeSection(d.global),
    volumeSection(d.global),
    emojiSection(d.global),
    reactionSection(d.global),
    attachmentSection(d.global),
    habitsSection(d.global),
    callSection(d.calls),
    groupSection(d),
  ];
  parts.forEach((p) => p && sections.appendChild(p));
  reveal(sections);

  scrollTo(0, 0);
  // The cue only helps until you've scrolled once.
  $('cue').classList.remove('gone');
  addEventListener('scroll', () => $('cue').classList.add('gone'), { once: true });
}

$('export').onclick = () => { location.href = '/api/export' + yQuery(YEAR); };
$('saveHtml').onclick = () => { location.href = '/api/save' + yQuery(YEAR); };

function countUpEl(el, to, suffix) {
  if (REDUCED) { el.textContent = num(to) + suffix; return; }
  const start = performance.now(), ms = 1400;
  (function frame(t) {
    const p = Math.min(1, (t - start) / ms);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = num(to * eased) + (p === 1 ? suffix : '');
    if (p < 1) requestAnimationFrame(frame);
  })(start);
}

/* ---- generate + progress -------------------------------------------------- */
let timer = null, target = 0, shown = 0;

/* The analysis takes a couple of seconds, so polling alone gives the bar only a
   handful of samples and it lurches. Ease toward the last real number every frame:
   this interpolates between true values, it never runs ahead of them. */
function tick() {
  shown += (target - shown) * 0.12;
  if (target - shown < 0.15) shown = target;
  $('fill').style.width = shown.toFixed(2) + '%';
  $('bar').setAttribute('aria-valuenow', Math.round(shown));
  if (shown < 100) requestAnimationFrame(tick);
}

/* Building a year we haven't built yet always goes through here, whether it was
   started from the landing page or from the switcher in the report — so a re-run
   gets the same real progress bar rather than a frozen chip. */
async function generate(y) {
  YEAR = y;
  $('report').style.display = 'none';
  $('landing').style.display = '';
  $('go').disabled = true; $('bar').style.display = 'block'; $('err').style.display = 'none';
  drawPicker();
  target = shown = 0;
  clearInterval(timer);
  await fetch('/api/generate' + yQuery(y), { method:'POST' });
  requestAnimationFrame(tick);
  timer = setInterval(poll, 250);
  poll();
}

$('go').onclick = () => generate(YEAR);

async function poll() {
  const s = await (await fetch('/api/status')).json();
  target = Math.max(target, s.progress);
  $('step').textContent = s.step ? s.step + '…' : '';
  if (s.state === 'done') {
    clearInterval(timer); $('step').textContent = 'Ready.';
    const r = await (await fetch('/api/report' + yQuery(YEAR))).json();
    setTimeout(() => render(r), 320);
  }
  if (s.state === 'error') {
    clearInterval(timer); $('step').textContent = ''; target = shown = 0;
    $('err').style.display = 'block'; $('err').textContent = s.error;
    $('go').disabled = false;
    drawPicker();
  }
}

/* ---- start ---------------------------------------------------------------- */
if (EMBEDDED) {
  // A saved copy: there is no server behind this file, so skip the landing page and
  // hide the controls that would need one.
  $('saveHtml').style.display = 'none';
  $('export').style.display = 'none';
  $('foot').textContent =
    'A saved copy of a Wrapped built on ' + shortDate(EMBEDDED.meta.generated_at, true) +
    '. Everything in it was computed locally; nothing was ever uploaded.';
  render(EMBEDDED);
} else {
  loadYears();
}
</script></body></html>"""

# The saved-copy export swaps this exact line for the report itself. Kept as a named
# constant so a rename in the page can never silently turn the export into a no-op.
EMBED_MARKER = "const EMBEDDED = null;"
assert EMBED_MARKER in PAGE_HTML


def standalone_html(payload: str, year: Optional[int]) -> bytes:
    """The page with the report baked in — a file that works from the Finder forever.

    Every '<' in the JSON is escaped on the way in. JSON only allows '<' inside string
    values, where \\u003c means exactly the same thing, so this changes nothing about
    the data while making it impossible for a message containing '</script>' to break
    out of the script block.
    """
    literal = payload.replace("<", "\\u003c")
    page = PAGE_HTML.replace(EMBED_MARKER, f"const EMBEDDED = {literal};", 1)
    label = "" if year is None else f" {year}"
    page = page.replace(
        "<title>iMessage Wrapped</title>", f"<title>iMessage Wrapped{label}</title>", 1
    )
    return page.encode("utf-8")


# The analysis thread writes here; request threads read it. Never touch it unlocked.
STATE_LOCK = threading.Lock()
STATE: dict = {"state": "idle", "progress": 0, "step": "", "error": None, "year": None}

# Reports are cached per year ("all", "2025", …) so switching the filter back to
# something you've already generated is instant instead of another full pass.
REPORTS: "dict[str, str]" = {}
YEARS_CACHE: "list[int]" = []


def year_key(year: Optional[int]) -> str:
    return "all" if year is None else str(year)


def years_available() -> "list[int]":
    """Calendar years the library covers, newest first — or nothing if we can't look.

    This is asked for before the user has clicked anything, so every failure (no Full
    Disk Access, no database, a locked WAL) has to come back as an empty list rather
    than an error: the landing page simply doesn't offer year chips and generates all
    of time. It also deliberately opens the file directly instead of going through
    open_db, so a page load can never trigger the 280 MB snapshot copy.
    """
    global YEARS_CACHE
    if YEARS_CACHE:
        return YEARS_CACHE
    try:
        con = sqlite3.connect(_ro_uri(CHAT_DB), uri=True, timeout=2.0)
    except (sqlite3.Error, OSError):
        return []
    try:
        con.row_factory = sqlite3.Row
        YEARS_CACHE = available_years(con)
    except (sqlite3.Error, OSError):
        YEARS_CACHE = []
    finally:
        con.close()
    return YEARS_CACHE


def parse_year(query: str) -> Optional[int]:
    """Read ?year=N from a query string. Anything unparseable means all time."""
    value = urllib.parse.parse_qs(query).get("year", [""])[0]
    if not value or value == "all":
        return None
    try:
        year = int(value)
    except ValueError:
        return None
    return year if 1990 < year < 2100 else None

# Progress is real, not a timer: each named step owns a slice of the bar, and the
# message-reading step (by far the longest) advances by actual rows scanned.
STEP_WEIGHTS = (
    ("Opening database", 4),
    ("Reading messages", 56),
    ("Reading attachments", 8),
    ("Crunching numbers", 20),
    ("Reading reactions", 4),
    ("Building your Wrapped", 8),
)
_STEP_START = {}
_acc = 0
for _name, _weight in STEP_WEIGHTS:
    _STEP_START[_name] = (_acc, _weight)
    _acc += _weight
del _acc, _name, _weight


def set_state(**changes) -> None:
    with STATE_LOCK:
        STATE.update(changes)


def read_state() -> dict:
    with STATE_LOCK:
        return dict(STATE)


def claim_run(year: Optional[int]) -> bool:
    """Mark a run as started, but only if one isn't already going.

    This has to happen under the lock in the request thread: if we left it to the
    analysis thread, two POSTs arriving together would both pass the check and do
    the whole job twice.
    """
    with STATE_LOCK:
        if STATE["state"] == "running":
            return False
        STATE.update(state="running", progress=0, step="Opening database", error=None,
                     year=year)
        return True


def _progress_for(step: str, fraction: float = 0.0) -> int:
    start, weight = _STEP_START.get(step, (0, 0))
    return min(99, int(start + weight * max(0.0, min(1.0, fraction))))


def run_analysis(year: Optional[int] = None) -> None:
    """Build the report on a background thread, publishing progress as it goes."""
    global YEARS_CACHE
    try:
        preflight()
        con = open_db(quiet=True)
        schema = Schema.detect(con)
        if not YEARS_CACHE:
            YEARS_CACHE = available_years(con)

        # Total rows is known up front, so row progress is a true fraction, not a guess.
        total = con.execute("SELECT COUNT(*) AS n FROM chat_message_join").fetchone()["n"] or 1

        def on_rows(seen: int) -> None:
            set_state(step="Reading messages", progress=_progress_for("Reading messages", seen / total))

        set_state(progress=_progress_for("Reading messages"), step="Reading messages")
        corpus = load_corpus(con, schema, progress=on_rows, year=year)

        def on_step(name: str, fraction: float = 0.0) -> None:
            set_state(step=name, progress=_progress_for(name, fraction))

        report = build_report(corpus, con, schema, progress=on_step, year=year,
                              years=YEARS_CACHE)
        con.close()

        # Serialise here, on this thread: if the report can't be encoded we want the
        # error surfaced as a failed generation, not as a broken HTTP response later.
        payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
        REPORTS[year_key(year)] = payload
        set_state(state="done", progress=100, step="Ready")
    except PreflightError as exc:
        set_state(state="error", step="", error=str(exc).strip())
    except Exception as exc:  # noqa: BLE001 — the browser is the only place to report this
        set_state(state="error", step="", error=f"{type(exc).__name__}: {exc}")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "iMessageWrapped"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 — silence the access log
        pass

    # -- helpers ----------------------------------------------------------------------

    def _host_ok(self) -> bool:
        """Only accept the loopback names we advertise. Blocks DNS rebinding."""
        host = (self.headers.get("Host") or "").strip()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code: int, body: bytes, ctype: str, extra: "Optional[dict]" = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Belt and braces: even if a future edit adds a URL, the page cannot phone home.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "script-src 'unsafe-inline'; connect-src 'self'",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _text(self, code: int, message: str) -> None:
        self._send(code, message.encode("utf-8"), "text/plain; charset=utf-8")

    # -- routes -----------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        if not self._host_ok():
            self._text(403, "Forbidden: this server only answers to localhost.\n")
            return
        parts = urllib.parse.urlparse(self.path)
        path, year = parts.path, parse_year(parts.query)

        if path == "/":
            self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(200, read_state())
        elif path == "/api/years":
            # Which years exist, and which of them are already built. The page uses the
            # second list to switch filters without kicking off another pass.
            self._json(200, {"years": years_available(), "ready": sorted(REPORTS)})
        elif path in ("/api/report", "/api/export", "/api/save"):
            payload = REPORTS.get(year_key(year))
            if not payload:
                self._json(404, {"error": "not ready"})
                return
            stamp = datetime.now().strftime("%Y-%m-%d")
            label = "" if year is None else f"-{year}"
            name = f"imessage-wrapped{label}-{stamp}"
            if path == "/api/save":
                self._send(
                    200, standalone_html(payload, year), "text/html; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="{name}.html"'},
                )
                return
            extra = None
            if path == "/api/export":
                extra = {"Content-Disposition": f'attachment; filename="{name}.json"'}
            self._send(
                200, payload.encode("utf-8"), "application/json; charset=utf-8", extra
            )
        else:
            self._text(404, "Not found\n")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._text(403, "Forbidden: this server only answers to localhost.\n")
            return
        # Drain the body regardless, or keep-alive desynchronises on the next request.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        parts = urllib.parse.urlparse(self.path)
        if parts.path != "/api/generate":
            self._text(404, "Not found\n")
            return
        year = parse_year(parts.query)
        if year_key(year) in REPORTS:
            # Already built this year — nothing to do, the page will just fetch it.
            set_state(state="done", progress=100, step="Ready", error=None, year=year)
            self._json(200, read_state())
            return
        if not claim_run(year):
            self._json(200, read_state())
            return
        threading.Thread(
            target=run_analysis, args=(year,), name="analysis", daemon=True
        ).start()
        self._json(202, read_state())


def bind_server() -> http.server.ThreadingHTTPServer:
    last: "Optional[OSError]" = None
    for port in range(PORT_FIRST, PORT_LAST + 1):
        try:
            return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            last = exc
    raise PreflightError(
        f"Ports {PORT_FIRST}-{PORT_LAST} are all in use ({last}). "
        "Close whatever is using them, or pass --port."
    )


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def serve(port: "Optional[int]" = None, open_browser: bool = True) -> int:
    if port is not None:
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            raise PreflightError(f"Could not bind 127.0.0.1:{port} ({exc}).")
    else:
        httpd = bind_server()

    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"\n  {APP_NAME} is running at {url}")
    print("  Everything stays on this Mac. Press Ctrl+C when you're done.\n")
    if open_browser:
        threading.Timer(BROWSER_DELAY, webbrowser.open, args=(url,)).start()

    # Closing the terminal sends SIGTERM, not SIGINT; route both to the same clean exit.
    try:
        signal.signal(signal.SIGTERM, _raise_interrupt)
    except ValueError:
        pass  # not the main thread — nothing to install, and nothing lost

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.shutdown()
        httpd.server_close()
        cleanup_temp()
    print("Done. Nothing was uploaded.")
    return 0


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="imessage_wrapped.py", description=f"{APP_NAME} — offline, local, read-only."
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="check DB access, schema, dates and text extraction, then exit",
    )
    parser.add_argument(
        "--dump-stats",
        action="store_true",
        help="print the full report as JSON to stdout, then exit",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="with --dump-stats, emit single-line JSON instead of indented",
    )
    parser.add_argument(
        "--year", type=int, default=None,
        help="with --dump-stats, limit the report to one calendar year",
    )
    parser.add_argument(
        "--port", type=int, default=None, help=f"serve on this port instead of {PORT_FIRST}"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="print the URL but don't open a browser"
    )
    args = parser.parse_args(argv)

    try:
        if args.selftest:
            return selftest()
        if args.dump_stats:
            if args.year is not None and not 1990 < args.year < 2100:
                raise PreflightError(f"--year {args.year} isn't a plausible calendar year.")
            return dump_stats(compact=args.compact, year=args.year)
        # The server starts without touching chat.db — permissions are checked when the
        # user clicks Generate, so the browser can show the Full Disk Access steps.
        return serve(port=args.port, open_browser=not args.no_browser)
    except PreflightError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        cleanup_temp()
        print("\nDone. Nothing was uploaded.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
