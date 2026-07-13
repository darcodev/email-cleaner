"""imaplib wrapper. Everything works in UIDs rather than sequence numbers,
so the mailbox shifting under us can't make us hit the wrong message.
Deleting means copy-to-Trash unless you ask for a permanent delete.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import re
import ssl
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .errors import CleanerError

# how many UIDs we put on a single IMAP command line. Bigger batches mean
# fewer round trips (faster scans/moves), kept comfortably under the command
# line length most servers accept.
FETCH_BATCH = 1000
STORE_BATCH = 500

_HEADER_FIELDS = "(FROM SUBJECT DATE LIST-UNSUBSCRIBE)"
_FETCH_PARTS = f"(UID RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS {_HEADER_FIELDS}])"

_UID_RE = re.compile(rb"UID (\d+)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
_UNSUB_URL_RE = re.compile(r"<([^>]+)>")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# How many octets of the first body part we ask for when snippets are enabled.
# A short slice is enough for the model and keeps this far from "download bodies".
SNIPPET_OCTETS = 400


@dataclass
class EmailSummary:
    uid: str
    sender_name: str
    sender_email: str
    subject: str
    date: str
    size: int
    flagged: bool
    unsubscribe: list[str] = field(default_factory=list)

    @property
    def sender_display(self) -> str:
        if self.sender_name and self.sender_email:
            return f"{self.sender_name} <{self.sender_email}>"
        return self.sender_email or self.sender_name or "(unknown sender)"


def decode_mime_header(raw: str) -> str:
    """Decode RFC 2047 encoded-words ('=?UTF-8?B?...?=') into readable text."""
    if not raw:
        return ""
    parts = []
    try:
        for chunk, charset in email.header.decode_header(raw):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
    except Exception:
        return raw
    return "".join(parts).strip()


def extract_unsubscribe_urls(header_value: str) -> list[str]:
    """Pull URLs out of a List-Unsubscribe header.

    The header looks like: <https://ex.com/unsub?x=1>, <mailto:unsub@ex.com>
    """
    if not header_value:
        return []
    urls = [u.strip() for u in _UNSUB_URL_RE.findall(header_value)]
    # https links first, mailto as a fallback
    urls.sort(key=lambda u: (not u.startswith("http"), u))
    return [u for u in urls if u]


def quote_imap_string(value: str) -> str:
    r"""Quote a string for use in an IMAP command ('a"b' -> '"a\"b"')."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_fetch_response(data: list) -> list[EmailSummary]:
    """Turn imaplib's FETCH response into EmailSummary objects.

    imaplib hands back a cursed mix of tuples (metadata, literal-bytes) and
    stray b')' seperators, we only care about the tuples.
    """
    summaries = []
    # print(data)  # left this in while i was figuring out the response shape
    for item in data:
        if not (isinstance(item, tuple) and len(item) >= 2):
            continue
        meta, header_bytes = item[0], item[1]
        if not isinstance(meta, bytes):
            continue

        uid_m = _UID_RE.search(meta)
        if not uid_m:
            continue
        size_m = _SIZE_RE.search(meta)
        flags_m = _FLAGS_RE.search(meta)
        flags = flags_m.group(1) if flags_m else b""

        msg = email.message_from_bytes(header_bytes or b"")
        sender_name, sender_email = email.utils.parseaddr(msg.get("From", ""))
        date_str = ""
        try:
            parsed = email.utils.parsedate_to_datetime(msg.get("Date", ""))
            if parsed:
                date_str = parsed.strftime("%Y-%m-%d")
        except Exception:
            date_str = (msg.get("Date") or "")[:10]

        summaries.append(
            EmailSummary(
                uid=uid_m.group(1).decode(),
                sender_name=decode_mime_header(sender_name),
                sender_email=sender_email.lower(),
                subject=decode_mime_header(msg.get("Subject", "")) or "(no subject)",
                date=date_str,
                size=int(size_m.group(1)) if size_m else 0,
                flagged=b"\\Flagged" in flags,
                unsubscribe=extract_unsubscribe_urls(msg.get("List-Unsubscribe", "")),
            )
        )
    return summaries


def _clean_snippet(raw: bytes) -> str:
    """Turn a raw body slice into a short, readable one-liner. Best effort: we
    do not fetch the part's transfer-encoding, so this strips obvious HTML tags
    and collapses whitespace rather than fully decoding the body."""
    text = (raw or b"").decode("utf-8", errors="replace")
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:SNIPPET_OCTETS]


def _parse_snippet_response(data: list) -> dict[str, str]:
    """Map uid -> cleaned snippet from a partial-body FETCH response."""
    out: dict[str, str] = {}
    for item in data:
        if not (isinstance(item, tuple) and len(item) >= 2):
            continue
        meta, body_bytes = item[0], item[1]
        if not isinstance(meta, bytes):
            continue
        uid_m = _UID_RE.search(meta)
        if not uid_m:
            continue
        snippet = _clean_snippet(body_bytes if isinstance(body_bytes, bytes) else b"")
        if snippet:
            out[uid_m.group(1).decode()] = snippet
    return out


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class ImapSession:
    """A logged-in IMAP connection plus the handful of ops we need."""

    def __init__(self, host: str, port: int, address: str, password: str):
        self.host = host
        self.port = port
        self.address = address
        self._password = password
        self._imap: imaplib.IMAP4_SSL | None = None
        self._capabilities: set[str] = set()

    def connect(self) -> None:
        try:
            self._imap = imaplib.IMAP4_SSL(
                self.host, self.port, ssl_context=ssl.create_default_context(), timeout=30
            )
        except (socket.gaierror, TimeoutError, OSError) as exc:
            raise CleanerError(
                f"Could not reach {self.host}:{self.port} ({exc}).",
                hint="Check your internet connection and the IMAP host name.",
            ) from exc
        except ssl.SSLError as exc:
            raise CleanerError(
                f"TLS handshake with {self.host} failed ({exc}).",
                hint="The server may not support implicit TLS on this port.",
            ) from exc

        try:
            self._imap.login(self.address, self._password)
        except imaplib.IMAP4.error as exc:
            raise CleanerError(
                f"Login failed for {self.address}.",
                hint=(
                    "Regular account passwords usually don't work over IMAP. "
                    "Make sure EMAIL_CLEANER_PASSWORD is an app password "
                    "(see the README for where to create one)."
                ),
            ) from exc

        # Some servers (Gmail included) only advertise their full capability
        # list after login, so refresh it now.
        try:
            typ, data = self._imap.capability()
            if typ == "OK" and data and data[0]:
                self._capabilities = {c.upper() for c in data[0].decode().split()}
        except imaplib.IMAP4.error:
            self._capabilities = {c.upper() for c in self._imap.capabilities}

    @property
    def supports_gmail_search(self) -> bool:
        return "X-GM-EXT-1" in self._capabilities

    @property
    def supports_move(self) -> bool:
        # RFC 6851. Gmail and most modern servers advertise it; it lets us
        # trash a batch in one round trip instead of copy + mark + expunge.
        return "MOVE" in self._capabilities

    def close(self) -> None:
        if self._imap is None:
            return
        try:
            if self._imap.state == "SELECTED":
                self._imap.close()
            self._imap.logout()
        except Exception:
            pass
        self._imap = None

    def _conn(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            raise CleanerError("Not connected. This is a bug, please report it.")
        return self._imap

    def select(self, folder: str = "INBOX", readonly: bool = True) -> int:
        """Open a folder, return how many messages it holds."""
        typ, data = self._conn().select(quote_imap_string(folder), readonly=readonly)
        if typ != "OK":
            detail = (data[0] or b"").decode(errors="replace") if data else ""
            raise CleanerError(
                f"Could not open folder '{folder}' ({detail}).",
                hint="Use --folder to pick a folder that exists on your server.",
            )
        try:
            return int(data[0])
        except (TypeError, ValueError):
            return 0

    def search_gmail_raw(self, query: str) -> list[str]:
        # X-GM-RAW lets us hand Gmail its own search-box syntax
        typ, data = self._conn().uid("SEARCH", "X-GM-RAW", quote_imap_string(query))
        return self._search_result(typ, data)

    def search_standard(self, criteria: list[str]) -> list[str]:
        typ, data = self._conn().uid("SEARCH", *criteria)
        return self._search_result(typ, data)

    @staticmethod
    def _search_result(typ: str, data: list) -> list[str]:
        if typ != "OK":
            detail = (data[0] or b"").decode(errors="replace") if data else ""
            raise CleanerError(f"Search failed ({detail}).")
        if not data or not data[0]:
            return []
        # UIDs come back oldest first; keep that order so --limit trims
        # the newest messages, not the oldest
        return data[0].decode().split()

    def fetch_summaries(
        self, uids: list[str], on_progress: Callable[[int, int], None] | None = None
    ) -> list[EmailSummary]:
        summaries: list[EmailSummary] = []
        done = 0
        for batch in _chunks(uids, FETCH_BATCH):
            typ, data = self._conn().uid("FETCH", ",".join(batch), _FETCH_PARTS)
            if typ != "OK":
                raise CleanerError("Fetching message headers failed.")
            summaries.extend(_parse_fetch_response(data))
            done += len(batch)
            if on_progress:
                on_progress(min(done, len(uids)), len(uids))
        return summaries

    def fetch_snippets(
        self, uids: list[str], on_progress: Callable[[int, int], None] | None = None
    ) -> dict[str, str]:
        """Fetch a short plain-text slice of each message's first body part.

        Only used for the opt-in --ai-snippet path. This is the one place we
        look past headers, and even then only at a bounded slice (never the
        whole body, never attachments). Anything that fails to fetch or parse is
        simply absent from the result, so classification falls back to headers.
        """
        snippets: dict[str, str] = {}
        done = 0
        part = f"(UID BODY.PEEK[1]<0.{SNIPPET_OCTETS}>)"
        for batch in _chunks(uids, FETCH_BATCH):
            typ, data = self._conn().uid("FETCH", ",".join(batch), part)
            if typ == "OK":
                snippets.update(_parse_snippet_response(data))
            done += len(batch)
            if on_progress:
                on_progress(min(done, len(uids)), len(uids))
        return snippets

    def find_trash_folder(self, hint: str | None = None) -> str:
        """Find the Trash folder: preset hint, then the \\Trash flag, then guesses."""
        conn = self._conn()

        candidates = [hint] if hint else []
        try:
            typ, listing = conn.list()
            if typ == "OK":
                for line in listing or []:
                    if not isinstance(line, bytes):
                        continue
                    text = line.decode(errors="replace")
                    if "\\Trash" in text:
                        # folder name is the last quoted string on the line
                        m = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
                        if m:
                            candidates.append(m[-1].replace('\\"', '"').replace("\\\\", "\\"))
        except imaplib.IMAP4.error:
            pass
        # TODO: some servers localize this ("Papelera", "Corbeille", ...) so this
        # list wont always hit. the \Trash flag above is the reliable path really
        candidates += ["Trash", "[Gmail]/Trash", "Deleted", "Deleted Items", "Deleted Messages"]

        for name in candidates:
            if not name:
                continue
            typ, _ = conn.status(quote_imap_string(name), "(MESSAGES)")
            if typ == "OK":
                return name
        raise CleanerError(
            "Could not find your Trash folder.",
            hint="Pass it explicitly with --trash-folder.",
        )

    def move_to_trash(
        self,
        uids: list[str],
        trash_folder: str,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Move messages to Trash.

        Uses the server's MOVE extension when it has one: a single command
        per batch that copies, marks deleted and expunges in one round trip.
        Older servers fall back to the manual copy + mark \\Deleted + expunge.
        """
        conn = self._conn()
        moved = 0
        quoted_trash = quote_imap_string(trash_folder)
        can_move = self.supports_move
        for batch in _chunks(uids, STORE_BATCH):
            uid_set = ",".join(batch)
            if can_move:
                typ, data = conn.uid("MOVE", uid_set, quoted_trash)
            else:
                typ, data = conn.uid("COPY", uid_set, quoted_trash)
            if typ != "OK":
                detail = (data[0] or b"").decode(errors="replace") if data else ""
                raise CleanerError(
                    f"Moving messages to '{trash_folder}' failed ({detail}). "
                    f"{moved} message(s) were moved before the error.",
                )
            if not can_move:
                conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
            moved += len(batch)
            if on_progress:
                on_progress(moved, len(uids))
        if not can_move:
            # MOVE expunges as it goes; only the copy fallback needs this
            conn.expunge()
        return moved

    def delete_permanently(
        self, uids: list[str], on_progress: Callable[[int, int], None] | None = None
    ) -> int:
        """Flag messages \\Deleted and expunge. Does not pass through Trash."""
        conn = self._conn()
        deleted = 0
        for batch in _chunks(uids, STORE_BATCH):
            typ, _ = conn.uid("STORE", ",".join(batch), "+FLAGS.SILENT", "(\\Deleted)")
            if typ != "OK":
                raise CleanerError(
                    f"Marking messages deleted failed. {deleted} message(s) "
                    "were deleted before the error."
                )
            deleted += len(batch)
            if on_progress:
                on_progress(deleted, len(uids))
        conn.expunge()
        return deleted

    def empty_trash(
        self, trash_folder: str, on_progress: Callable[[int, int], None] | None = None
    ) -> int:
        """Permanently remove everything currently in the Trash folder.

        Opens Trash read-write, flags every message \\Deleted and expunges.
        This is not recoverable, so callers should confirm first. Returns how
        many messages were removed.
        """
        conn = self._conn()
        self.select(trash_folder, readonly=False)
        uids = self.search_standard(["ALL"])
        if not uids:
            return 0
        removed = 0
        for batch in _chunks(uids, STORE_BATCH):
            typ, _ = conn.uid("STORE", ",".join(batch), "+FLAGS.SILENT", "(\\Deleted)")
            if typ != "OK":
                raise CleanerError(
                    f"Emptying '{trash_folder}' failed after {removed} message(s)."
                )
            removed += len(batch)
            if on_progress:
                on_progress(removed, len(uids))
        conn.expunge()
        return len(uids)

    def __enter__(self) -> "ImapSession":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
