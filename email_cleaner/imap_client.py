"""imaplib wrapper. Everything works in UIDs rather than sequence numbers,
so the mailbox shifting under us can't make us hit the wrong message.
Deleting means copy-to-Trash unless you ask for a permanent delete.
"""

from __future__ import annotations

import base64
import email
import email.header
import email.utils
import imaplib
import re
import ssl
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .errors import CleanerError, SearchUnsupported

# how many UIDs we put on a single IMAP command line. Bigger batches mean
# fewer round trips (faster scans/moves), kept comfortably under the command
# line length most servers accept.
FETCH_BATCH = 1000
STORE_BATCH = 500
# A full header block is an order of magnitude more data per message than the
# four fields we normally ask for, so those go in much smaller batches. Progress
# is reported per batch, and one batch of 1000 full headers is over half a
# minute of an entirely silent terminal - long enough to look hung and be killed.
FULL_FETCH_BATCH = 100

_HEADER_FIELDS = "(FROM SUBJECT DATE LIST-UNSUBSCRIBE)"
_FETCH_PARTS = f"(UID RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS {_HEADER_FIELDS}])"
# The whole header block, for servers that do not honour the field list above.
# Yahoo returns FROM/SUBJECT/DATE and silently drops LIST-UNSUBSCRIBE from it,
# which reads as "no marketing mail anywhere in this mailbox" - the one header
# the promotional filter runs on. Costs more bytes, so it is not the default.
_FETCH_PARTS_FULL = "(UID RFC822.SIZE FLAGS BODY.PEEK[HEADER])"

_MESSAGES_RE = re.compile(r"MESSAGES (\d+)")
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


def encode_mailbox_name(name: str) -> str:
    """Encode a folder name as modified UTF-7 (RFC 3501 section 5.1.3).

    IMAP folder names travel as ASCII, so 'Gelöschte' has to go over the wire
    as 'Gel&APY-schte'. An all-ASCII name is returned untouched on purpose:
    names we got back from LIST are already in this encoding, and re-encoding
    would turn their '&' into '&-' and point us at a folder that doesn't exist.
    """
    if name.isascii():
        return name
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        raw = "".join(pending).encode("utf-16-be")
        b64 = base64.b64encode(raw).decode("ascii").rstrip("=")
        out.append("&" + b64.replace("/", ",") + "-")
        pending.clear()

    for ch in name:
        if " " <= ch <= "~":  # printable ascii represents itself
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def quoted_mailbox(name: str) -> str:
    """A folder name ready to hand to imaplib: encoded, then quoted."""
    return quote_imap_string(encode_mailbox_name(name))


def search_args(criteria: list[str]) -> list:
    """Prepare SEARCH arguments so non-ASCII terms survive the trip.

    imaplib encodes str arguments as ASCII, so a keyword, sender or Gmail query
    containing an accent raises UnicodeEncodeError before the command is even
    sent. When anything needs more than ASCII we declare CHARSET UTF-8 and hand
    imaplib pre-encoded bytes, which it appends to the command line verbatim.
    Pure ASCII criteria are passed through unchanged so we keep talking to old
    servers exactly as before.
    """
    if all(c.isascii() for c in criteria):
        return list(criteria)
    return ["CHARSET", "UTF-8", *(c.encode("utf-8") for c in criteria)]


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
        self.selected_folder: str | None = None
        # set when the purge could not be scoped to our own UIDs; the CLI reads
        # it after the run so the user hears what else it may have taken out
        self.expunge_notice: str | None = None
        # what STATUS said a folder held just before we opened it. Yahoo gives
        # the real total until the mailbox is selected and the size of its own
        # windowed view afterwards, so open time is the only honest moment.
        self._folder_totals: dict[str, int] = {}

    def connect(self) -> None:
        try:
            self._imap = imaplib.IMAP4_SSL(
                self.host, self.port, ssl_context=ssl.create_default_context(), timeout=30
            )
        # ssl.SSLError is a subclass of OSError, so it has to be caught first.
        # Behind the reachability handler it was dead code, and every TLS
        # failure - a plaintext port like 143 answering the handshake, an
        # expired or untrusted certificate - came out as "could not reach
        # host", telling the user to check an internet connection that was
        # working fine and never mentioning the port they had actually mistyped.
        except ssl.SSLError as exc:
            raise CleanerError(
                f"TLS handshake with {self.host} failed ({exc}).",
                hint=(
                    "The server may not support implicit TLS on this port. "
                    "Most IMAP servers use 993; try --port 993."
                ),
            ) from exc
        except (socket.gaierror, TimeoutError, OSError) as exc:
            raise CleanerError(
                f"Could not reach {self.host}:{self.port} ({exc}).",
                hint="Check your internet connection and the IMAP host name.",
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

        self._load_capabilities()

    def _load_capabilities(self) -> None:
        """Work out what this server can do, erring towards what it told us.

        Some servers (Gmail included) only advertise their full list after
        login, so the post-login refresh is worth asking for - but it is only
        ever added to what imaplib already parsed from the greeting, never
        swapped in for it. A refresh that comes back NO, or OK with an empty
        payload, used to leave us believing the server supports nothing at all:
        Gmail search off (so a different set of mail matches), MOVE off, and
        worst of all UID EXPUNGE off, which drops the purge to the unscoped
        bare EXPUNGE that takes other clients' pending deletions with it.
        """
        conn = self._conn()
        self._capabilities = {c.upper() for c in conn.capabilities}
        try:
            typ, data = conn.capability()
            if typ == "OK" and data and data[0]:
                self._capabilities |= {c.upper() for c in data[0].decode().split()}
        except imaplib.IMAP4.error:
            pass

    @property
    def supports_gmail_search(self) -> bool:
        return "X-GM-EXT-1" in self._capabilities

    @property
    def supports_move(self) -> bool:
        # RFC 6851. Gmail and most modern servers advertise it; it lets us
        # trash a batch in one round trip instead of copy + mark + expunge.
        return "MOVE" in self._capabilities

    @property
    def supports_uid_expunge(self) -> bool:
        # RFC 4315. Without it the only way to purge is a bare EXPUNGE, which
        # is not scoped to our messages - see _expunge below.
        return "UIDPLUS" in self._capabilities

    @property
    def supports_unselect(self) -> bool:
        # RFC 3691. Leaves the mailbox without the implicit purge CLOSE does.
        return "UNSELECT" in self._capabilities

    def close(self) -> None:
        """Hang up. Deliberately never sends IMAP CLOSE.

        CLOSE looks like the polite way out, but on a writable mailbox it
        permanently removes every \\Deleted message in it first - the same
        unscoped purge _expunge goes out of its way to avoid. 'clean' opens the
        folder read-write, so ending that way would quietly destroy mail another
        client had flagged and not compacted, with no Trash copy and none of the
        warning the bare-EXPUNGE path prints. UNSELECT (RFC 3691) is CLOSE
        without the purge; where it isn't offered, LOGOUT from the selected
        state ends the session just as cleanly and expunges nothing.
        """
        if self._imap is None:
            return
        try:
            if self._imap.state == "SELECTED" and self.supports_unselect:
                self._imap.unselect()
            self._imap.logout()
        except Exception:
            pass
        self._imap = None

    def _conn(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            raise CleanerError("Not connected. This is a bug, please report it.")
        return self._imap

    def select(self, folder: str = "INBOX", readonly: bool = True) -> int:
        """Open a folder, return how many messages the selected view exposes.

        That is not always the same as how many the folder holds - see
        folder_message_count, which is why the count is taken first.
        """
        held = self._status_message_count(folder)
        typ, data = self._conn().select(quoted_mailbox(folder), readonly=readonly)
        if typ != "OK":
            detail = (data[0] or b"").decode(errors="replace") if data else ""
            raise CleanerError(
                f"Could not open folder '{folder}' ({detail}).",
                hint="Use --folder to pick a folder that exists on your server.",
            )
        self.selected_folder = folder
        if held is not None:
            self._folder_totals[folder] = held
        try:
            return int(data[0])
        except (TypeError, ValueError):
            return 0

    def search_gmail_raw(self, query: str) -> list[str]:
        # X-GM-RAW lets us hand Gmail its own search-box syntax
        args = search_args(["X-GM-RAW", quote_imap_string(query)])
        typ, data = self._uid_search(args)
        return self._search_result(typ, data)

    def search_standard(self, criteria: list[str]) -> list[str]:
        typ, data = self._uid_search(search_args(criteria))
        return self._search_result(typ, data)

    def _uid_search(self, args: list) -> tuple:
        """Run UID SEARCH, turning imaplib's exceptions into our own.

        imaplib raises on a BAD reply from inside _command_complete, so the
        status never reaches _search_result and its friendly message. A server
        refusing a search term it does not implement answers exactly that way -
        Yahoo returns "[CANNOT] ... not supported" for any HEADER search - and
        the user got a raw traceback for something the tool can work around.
        """
        try:
            return self._conn().uid("SEARCH", *args)
        except imaplib.IMAP4.abort as exc:
            # the connection itself is gone; retrying on it would only fail again
            raise CleanerError(
                f"The connection to {self.host} dropped mid-search ({exc}).",
                hint="Run it again; if it keeps happening the server may be rate limiting.",
            ) from exc
        except imaplib.IMAP4.error as exc:
            raise SearchUnsupported(
                f"The server rejected this search ({exc}).",
                hint="This server does not implement every IMAP search term.",
            ) from exc

    @staticmethod
    def _search_result(typ: str, data: list) -> list[str]:
        if typ != "OK":
            detail = (data[0] or b"").decode(errors="replace") if data else ""
            hint = None
            if "BADCHARSET" in detail.upper():
                # we only ask for UTF-8 when a term needs it, so this is a
                # server that cannot search outside ascii at all
                hint = "This server can't search non-ASCII text; try an ASCII keyword."
            raise CleanerError(f"Search failed ({detail}).", hint=hint)
        if not data or not data[0]:
            return []
        # UIDs come back oldest first; keep that order so --limit trims
        # the newest messages, not the oldest
        return data[0].decode().split()

    def fetch_summaries(
        self,
        uids: list[str],
        on_progress: Callable[[int, int], None] | None = None,
        full_headers: bool = False,
    ) -> list[EmailSummary]:
        """Header summaries for `uids`.

        full_headers asks for the entire header block instead of the four
        fields we actually read. Only worth it when the caller depends on
        List-Unsubscribe and the server cannot be trusted to include it in a
        HEADER.FIELDS list - see _FETCH_PARTS_FULL.
        """
        summaries: list[EmailSummary] = []
        done = 0
        parts = _FETCH_PARTS_FULL if full_headers else _FETCH_PARTS
        size = FULL_FETCH_BATCH if full_headers else FETCH_BATCH
        # say we have started before the first batch, not after it: on a slow
        # server the first chunk is seconds of otherwise blank terminal, which
        # is exactly long enough for someone to conclude it has hung
        if on_progress and uids:
            on_progress(0, len(uids))
        for batch in _chunks(uids, size):
            typ, data = self._conn().uid("FETCH", ",".join(batch), parts)
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

    def _status_message_count(self, folder: str) -> int | None:
        """Ask STATUS how many messages a folder holds. None if it will not say."""
        try:
            typ, data = self._conn().status(quoted_mailbox(folder), "(MESSAGES)")
        except imaplib.IMAP4.error:
            return None
        if typ != "OK" or not data or not data[0]:
            return None
        found = _MESSAGES_RE.search(data[0].decode(errors="replace"))
        return int(found.group(1)) if found else None

    def folder_message_count(self, folder: str) -> int | None:
        """How many messages the folder held when we opened it.

        Worth knowing separately from what a search returns: the two disagree
        on Yahoo, which reports a 235k-message Inbox and then exposes only the
        newest 10000 of it to the selected view. Every search runs against the
        smaller number, so a run that finds nothing is not evidence the mail is
        not there. Asked before SELECT and remembered, because afterwards the
        server answers with the size of the window instead of the folder.
        """
        if folder in self._folder_totals:
            return self._folder_totals[folder]
        return self._status_message_count(folder)

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
            typ, _ = conn.status(quoted_mailbox(name), "(MESSAGES)")
            if typ == "OK":
                return name
        raise CleanerError(
            "Could not find your Trash folder.",
            hint="Pass it explicitly with --trash-folder.",
        )

    def _expunge(self, uids: list[str]) -> None:
        """Purge the messages we just flagged \\Deleted - and only those.

        A bare EXPUNGE removes *every* \\Deleted message in the open folder,
        including ones some other client flagged and never purged (Thunderbird
        and Outlook both leave mail in that state until you compact). That mail
        was never ours to touch and there is no Trash copy of it, so we use UID
        EXPUNGE (RFC 4315) to name exactly our own UIDs. Servers without UIDPLUS
        leave us no choice but the blunt version; we note that for the CLI.
        """
        if not uids:
            return
        conn = self._conn()
        if self.supports_uid_expunge:
            for batch in _chunks(uids, STORE_BATCH):
                typ, _ = conn.uid("EXPUNGE", ",".join(batch))
                if typ != "OK":
                    # they are flagged, just still there; the next run picks
                    # them up. Better than reporting a delete that didn't happen
                    self.expunge_notice = (
                        "the server refused UID EXPUNGE, so some messages are "
                        "marked deleted but still in the folder"
                    )
            return
        self.expunge_notice = (
            "this server has no UID EXPUNGE, so any messages another mail app "
            "had already flagged deleted in this folder were purged too"
        )
        conn.expunge()

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
        quoted_trash = quoted_mailbox(trash_folder)
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
                # the copy landed, so a failure here leaves the message in both
                # places - worth stopping for rather than quietly duplicating
                typ, _ = conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
                if typ != "OK":
                    raise CleanerError(
                        f"Copied {len(batch)} message(s) to '{trash_folder}' but "
                        f"could not remove them from '{self.selected_folder}', so "
                        "they are now in both places.",
                        hint="Delete the copies from your Trash and try again.",
                    )
            moved += len(batch)
            if on_progress:
                on_progress(moved, len(uids))
        if not can_move:
            # MOVE expunges as it goes; only the copy fallback needs this
            self._expunge(uids)
        return moved

    def delete_permanently(
        self, uids: list[str], on_progress: Callable[[int, int], None] | None = None
    ) -> int:
        """Flag messages \\Deleted and purge them. Does not pass through Trash."""
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
        self._expunge(uids)
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
        # every message in the folder is flagged at this point, so the unscoped
        # EXPUNGE is exactly what was asked for here - nothing else can be hit
        conn.expunge()
        return len(uids)

    def __enter__(self) -> "ImapSession":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
