"""Figures out which messages match the user's filters.

On Gmail we use their own search syntax over IMAP (same engine as the
search box), so category:promotions etc. just works. On any other server
we fall back to plain IMAP SEARCH, using the List-Unsubscribe header as
the "this is marketing mail" signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .errors import CleanerError, SearchUnsupported
from .imap_client import (
    FULL_FETCH_BATCH,
    EmailSummary,
    ImapSession,
    quote_imap_string,
)

VALID_CATEGORIES = ("promotions", "social", "updates", "forums")

# appended to the query description when the server could not run the
# List-Unsubscribe test and we had to apply it to the fetched headers instead
_PROMO_LOCAL_NOTE = "(unsubscribe header checked on this machine)"

# month names for the IMAP date format (02-Jul-2026), locale-independent
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _clean_terms(values: list[str]) -> list[str]:
    """Strip every term and drop the blank ones."""
    return [v.strip() for v in values if v.strip()]


@dataclass
class Filters:
    """What counts as deletable. Defaults are deliberately cautious."""

    older_than_days: int = 30
    categories: list[str] = field(default_factory=lambda: ["promotions"])
    keywords: list[str] = field(default_factory=list)
    from_senders: list[str] = field(default_factory=list)
    protected_senders: list[str] = field(default_factory=list)
    promo_only: bool = True  # False = --all, don't require promotional
    include_starred: bool = False
    limit: int | None = None

    def __post_init__(self) -> None:
        """Normalize the term lists once, here, so nothing downstream has to.

        A blank term is not the harmless no-op it looks like. IMAP's TEXT ""
        is contained in every message, so a single empty --keyword - an unset
        shell variable in a cron line is all it takes - quietly turns a narrow
        search into the whole folder. It also slips past the --all "this matches
        everything" warning in cli, because a [""] list is still truthy.

        Padding is just as bad on the Gmail side: '-from: amazon.com' splits
        into an empty exclusion plus a *required* keyword, so a protect entry
        ends up widening the search it was supposed to narrow.
        """
        self.keywords = _clean_terms(self.keywords)
        self.from_senders = _clean_terms(self.from_senders)
        self.protected_senders = _clean_terms(self.protected_senders)


def parse_age(text: str) -> int:
    """'30d' -> 30, '3m' -> 90, '2y' -> 730, '0' -> no age filter."""
    text = text.strip().lower()
    if not text:
        raise CleanerError(
            "No age given.",
            hint="Use a number plus d/m/y, e.g. 30d, 3m, 2y. Use 0 for no age limit.",
        )
    if text in ("0", "0d", "all", "any"):
        return 0
    unit = text[-1]
    number = text[:-1] if unit in "dmy" else text
    try:
        value = int(number)
    except ValueError:
        raise CleanerError(
            f"Could not understand age '{text}'.",
            hint="Use a number plus d/m/y, e.g. 30d, 3m, 2y. Use 0 for no age limit.",
        ) from None
    if value < 0:
        raise CleanerError(f"Age can't be negative: '{text}'.")
    multiplier = {"d": 1, "m": 30, "y": 365}.get(unit, 1)
    return value * multiplier


def imap_date(days_ago: int, today: datetime | None = None) -> str:
    """The IMAP-format date `days_ago` days before today (02-Jul-2026).

    An absurd age pushes that subtraction past the earliest date Python can
    represent: '--older-than 3000y' - or a fat-fingered '--older-than 99999999'
    - raised OverflowError and a raw traceback for something the user typed.
    Clamp instead, because "older than the dawn of time" is a perfectly well
    defined search: it matches nothing, which is what was asked for.
    """
    now = today or datetime.now()
    try:
        day = now - timedelta(days=days_ago)
    except OverflowError:
        day = datetime.min
    # 4-digit year: the clamp lands in year 1, and IMAP's date-year is 4DIGIT
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year:04d}"


def _gmail_term(word: str) -> str:
    # phrases need quotes in gmail search
    return f'"{word}"' if " " in word else word


def build_gmail_query(filters: Filters) -> str:
    parts = []
    if filters.promo_only:
        cats = [c for c in filters.categories if c in VALID_CATEGORIES]
        if not cats:
            raise CleanerError(
                f"No valid categories in {filters.categories}.",
                hint=f"Valid categories: {', '.join(VALID_CATEGORIES)}",
            )
        if len(cats) == 1:
            parts.append(f"category:{cats[0]}")
        else:
            parts.append("(" + " OR ".join(f"category:{c}" for c in cats) + ")")
    if filters.older_than_days > 0:
        parts.append(f"older_than:{filters.older_than_days}d")
    if filters.keywords:
        terms = [_gmail_term(k) for k in filters.keywords]
        parts.append(terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")")
    if filters.from_senders:
        terms = [f"from:{_gmail_term(s)}" for s in filters.from_senders]
        parts.append(terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")")
    if not filters.include_starred:
        parts.append("-is:starred")
        parts.append("-is:important")
    for sender in filters.protected_senders:
        # quoted like the other terms: a bare '-from:big corp' would exclude
        # nothing and then require 'corp' to be in the message
        parts.append(f"-from:{_gmail_term(sender)}")
    return " ".join(parts)


def _or_group(key: str, values: list[str]) -> list[str]:
    """IMAP's OR is a binary prefix operator, so 'a or b or c' becomes
    OR a OR b c. Stacking the ORs up front works out to the same thing."""
    crit = ["OR"] * (len(values) - 1)
    for v in values:
        crit += [key, quote_imap_string(v)]
    return crit


def build_standard_criteria(filters: Filters, promo_via_header: bool = True) -> list[str]:
    """Plain IMAP SEARCH criteria for servers without gmail extensions.

    promo_via_header=False leaves the List-Unsubscribe test out, for servers
    that refuse HEADER searches. scan() then applies that half of the filter
    to the fetched headers instead - see _PROMO_LOCAL_NOTE.
    """
    criteria = []
    if filters.promo_only and promo_via_header:
        criteria += ["HEADER", "List-Unsubscribe", '""']
    if filters.older_than_days > 0:
        criteria += ["BEFORE", imap_date(filters.older_than_days)]
    if filters.keywords:
        criteria += _or_group("TEXT", filters.keywords)
    if filters.from_senders:
        criteria += _or_group("FROM", filters.from_senders)
    if not filters.include_starred:
        criteria += ["UNFLAGGED"]
    if not criteria:
        criteria = ["ALL"]
    return criteria


def is_protected(summary: EmailSummary, protected_senders: list[str]) -> bool:
    """Substring match on the sender address, so 'work.com' protects the
    whole domain and 'boss@work.com' protects one person."""
    sender = summary.sender_email.lower()
    # strip before matching too, not just for the blank-check, so a padded
    # entry like " amazon.com" still protects instead of silently missing
    hits = [p for p in protected_senders if p.strip() and p.strip().lower() in sender]
    return len(hits) > 0


@dataclass
class ScanResult:
    emails: list[EmailSummary]
    query_description: str
    skipped_protected: int = 0
    skipped_starred: int = 0
    skipped_ai: int = 0
    # non-promotional mail dropped here rather than by the server, on servers
    # that cannot run the List-Unsubscribe HEADER search themselves
    skipped_not_promo: int = 0
    # uid -> the model's one-line reason, for the messages it kept for deletion
    ai_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.emails)


def _apply_limit(items: list, limit: int | None) -> list:
    """Keep at most `limit` matches (the oldest, since UIDs and the summaries
    built from them are both oldest-first). None means no cap; 0 means keep
    none. Negatives clamp to 0."""
    if limit is None:
        return items
    return items[: max(limit, 0)]


def _drop_reason(summary: EmailSummary, filters: Filters, local_promo: bool) -> str | None:
    """Why this message is not a candidate, or None if it survives.

    One place, so the early-exit fetch below and the tally in scan() cannot
    drift apart about what counts as a match.
    """
    if local_promo and not summary.unsubscribe:
        return "not_promo"
    if is_protected(summary, filters.protected_senders):
        return "protected"
    if summary.flagged and not filters.include_starred:
        return "starred"
    return None


def _fetch_until_enough(session, uids: list[str], filters: Filters, on_progress):
    """Fetch full headers in chunks, stopping once --limit is satisfied.

    Only for the fallback path, where the server hands back every candidate and
    the promotional test happens here. A small --limit is usually satisfied in
    the first chunk, so fetching all of them - at roughly twelve times the bytes
    of a normal header fetch - is a long wait for a result already in hand.
    """
    wanted = max(filters.limit or 0, 0)
    out: list[EmailSummary] = []
    survivors = 0
    # this path drives progress itself, so it owns the opening tick too -
    # otherwise the first chunk is several more seconds of blank terminal
    if on_progress and uids:
        on_progress(0, len(uids))
    for start in range(0, len(uids), FULL_FETCH_BATCH):
        chunk = uids[start : start + FULL_FETCH_BATCH]
        batch = session.fetch_summaries(chunk, full_headers=True)
        out.extend(batch)
        survivors += sum(1 for s in batch if _drop_reason(s, filters, True) is None)
        if on_progress:
            on_progress(min(start + len(chunk), len(uids)), len(uids))
        if survivors >= wanted:
            break
    return out


def _apply_ai(candidates, classifier, snippets, on_progress):
    """Narrow the candidate set with the model's keep-or-delete verdicts.

    Kept for deletion only when the model explicitly said delete; a keep, an
    unknown uid, or a failed batch (empty verdicts) all default to keep-in-
    mailbox. Returns (to_clean, skipped_count, {uid: reason})."""
    verdicts = classifier.classify(candidates, snippets=snippets, on_progress=on_progress)
    to_clean: list[EmailSummary] = []
    reasons: dict[str, str] = {}
    skipped = 0
    for s in candidates:
        v = verdicts.get(s.uid)
        if v is not None and v.delete:
            to_clean.append(s)
            if v.reason:
                reasons[s.uid] = v.reason
        else:
            skipped += 1
    return to_clean, skipped, reasons


def scan(
    session: ImapSession,
    filters: Filters,
    on_progress=None,
    classifier=None,
    on_ai_progress=None,
    on_notice=None,
) -> ScanResult:
    """Search the selected folder and return everything that matched.

    When a classifier is given, an AI keep-or-delete pass runs after the normal
    safety net (protected/starred), so only the already-narrowed candidates are
    ever sent to it. The pass can only narrow the set further, never widen it.
    """
    local_promo = False  # set when the server made us filter promos on our side
    if session.supports_gmail_search:
        query = build_gmail_query(filters)
        if query:
            uids = session.search_gmail_raw(query)
            description = f"Gmail search: {query}"
        else:
            # nothing to filter on at all, just take the whole folder
            uids = session.search_standard(["ALL"])
            description = "all messages in the folder"
    else:
        criteria = build_standard_criteria(filters)
        try:
            uids = session.search_standard(criteria)
        except SearchUnsupported:
            # Yahoo, and any other server without HEADER search, refuses the
            # List-Unsubscribe test outright. That test is the whole of our
            # "this is promotional" rule off Gmail, so rather than fail the run
            # we search on what the server does accept and apply the header
            # half ourselves, below. It costs a wider header fetch, because the
            # servers that land here are also the ones that leave the header
            # out of a HEADER.FIELDS reply.
            if not filters.promo_only:
                raise  # nothing to fall back to; the terms themselves are refused
            if on_notice:
                # the retry and the wider fetch it forces are most of the wait
                # before anything else prints, so say why it is happening
                on_notice(
                    "This server can't search message headers, so the "
                    "promotional filter runs here instead - that means reading "
                    "more headers, which takes a little longer."
                )
            criteria = build_standard_criteria(filters, promo_via_header=False)
            uids = session.search_standard(criteria)
            local_promo = True
        description = "IMAP search: " + " ".join(criteria)
        if local_promo:
            description += " " + _PROMO_LOCAL_NOTE

    # with the fallback the server hands back more than we want, so the cap has
    # to wait until the non-promotional mail has been dropped - otherwise
    # --limit 100 would trim to 100 candidates and then keep only the promos
    # among them, which is not what "stop after this many matches" promises
    if not local_promo:
        uids = _apply_limit(uids, filters.limit)

    # the fallback filters on List-Unsubscribe here, so it needs a fetch that
    # actually carries it - the narrow field list comes back without it on the
    # very servers that send us down this path
    if local_promo and filters.limit is not None:
        summaries = _fetch_until_enough(session, uids, filters, on_progress)
    else:
        summaries = session.fetch_summaries(
            uids, on_progress=on_progress, full_headers=local_promo
        )

    # belt and suspenders: the server already filtered, but re-check protect
    # and starred here in case its search handed back something it shouldnt.
    # single pass keeps survivors and tallies drops at once, so is_protected
    # runs once per message instead of up to three times.
    kept: list[EmailSummary] = []
    dropped = {"not_promo": 0, "protected": 0, "starred": 0}
    for s in summaries:
        reason = _drop_reason(s, filters, local_promo)
        if reason:
            dropped[reason] += 1
            continue
        kept.append(s)
    skipped_protected = dropped["protected"]
    skipped_starred = dropped["starred"]
    skipped_not_promo = dropped["not_promo"]

    if local_promo:
        kept = _apply_limit(kept, filters.limit)

    # AI pass runs last and only on the survivors, so protected and starred mail
    # is never sent to it (and a hosted backend only ever sees the candidates).
    skipped_ai = 0
    ai_reasons: dict[str, str] = {}
    if classifier is not None and kept:
        snippets = {}
        if classifier.wants_snippet:
            snippets = session.fetch_snippets([s.uid for s in kept])
        kept, skipped_ai, ai_reasons = _apply_ai(kept, classifier, snippets, on_ai_progress)

    return ScanResult(
        emails=kept,
        query_description=description,
        skipped_protected=skipped_protected,
        skipped_starred=skipped_starred,
        skipped_ai=skipped_ai,
        skipped_not_promo=skipped_not_promo,
        ai_reasons=ai_reasons,
    )


def summarize_senders(emails: list[EmailSummary], top: int = 10) -> list[tuple[str, int, int]]:
    """[(sender, message count, total bytes)] with the noisiest first."""
    # one pass: tally count + bytes per sender. the dict keeps first-seen
    # order, and sorted() below is stable, so ties rank by who showed up first
    stats: dict[str, list[int]] = {}
    for e in emails:
        entry = stats.get(e.sender_display)
        if entry is None:
            stats[e.sender_display] = [1, e.size]
        else:
            entry[0] += 1
            entry[1] += e.size
    ranked = sorted(stats.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(sender, count, size) for sender, (count, size) in ranked[:top]]


def collect_unsubscribe_links(emails: list[EmailSummary]) -> list[tuple[str, int, str]]:
    by_sender: dict[str, tuple[int, str]] = {}
    for e in emails:
        if not e.unsubscribe:
            continue
        count, url = by_sender.get(e.sender_email, (0, e.unsubscribe[0]))
        by_sender[e.sender_email] = (count + 1, url)
    ranked = sorted(by_sender.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(sender, count, url) for sender, (count, url) in ranked]
