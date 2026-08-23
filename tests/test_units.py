"""Tests for the pure functions, no network needed.

Run with:  python -m unittest discover -s tests -v
"""

import dataclasses
import imaplib
import json
import os
import socket
import ssl
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from email_cleaner import cli, config
from email_cleaner.ai import (
    AISettings,
    Classifier,
    Verdict,
    _BackendError,
    _extract_text,
    _loads_lenient,
    _parse_verdicts,
)
from email_cleaner.errors import CleanerError, SearchUnsupported
from email_cleaner.imap_client import (
    EmailSummary,
    ImapSession,
    _clean_snippet,
    _parse_fetch_response,
    _parse_snippet_response,
    decode_mime_header,
    encode_mailbox_name,
    extract_unsubscribe_urls,
    quote_imap_string,
    quoted_mailbox,
    search_args,
)
from email_cleaner.providers import get_provider, guess_provider
from email_cleaner.scanner import (
    Filters,
    _apply_ai,
    _apply_limit,
    build_gmail_query,
    build_standard_criteria,
    imap_date,
    is_protected,
    parse_age,
    scan,
    summarize_senders,
)
from email_cleaner.ui import colors_enabled, human_size, progress, truncate


def _with_date(summary, date):
    return dataclasses.replace(summary, date=date)


def _mail(uid, sender="Shop <deals@shop.com>", subject="Sale!", unsub=None):
    name, _, addr = sender.partition(" <")
    return EmailSummary(
        uid=uid,
        sender_name=name,
        sender_email=addr.rstrip(">") or name,
        subject=subject,
        date="2026-01-01",
        size=100,
        flagged=False,
        unsubscribe=unsub or [],
    )


class TestParseAge(unittest.TestCase):
    def test_days(self):
        self.assertEqual(parse_age("30d"), 30)

    def test_months(self):
        self.assertEqual(parse_age("3m"), 90)

    def test_years(self):
        self.assertEqual(parse_age("2y"), 730)

    def test_bare_number_means_days(self):
        self.assertEqual(parse_age("45"), 45)

    def test_zero_and_aliases(self):
        for text in ("0", "0d", "all", "any"):
            self.assertEqual(parse_age(text), 0)

    def test_garbage_raises_friendly_error(self):
        with self.assertRaises(CleanerError):
            parse_age("soon")

    def test_negative_raises(self):
        with self.assertRaises(CleanerError):
            parse_age("-5d")

    def test_empty_raises_friendly_error(self):
        for text in ("", "   "):
            with self.assertRaises(CleanerError):
                parse_age(text)


class TestImapDate(unittest.TestCase):
    def test_format_is_locale_independent(self):
        self.assertEqual(imap_date(30, today=datetime(2026, 7, 2)), "02-Jun-2026")

    def test_year_rollover(self):
        self.assertEqual(imap_date(365, today=datetime(2026, 1, 1)), "01-Jan-2025")

    def test_an_absurd_age_clamps_instead_of_raising(self):
        # '--older-than 3000y', or a fat-fingered day count, walked off the end
        # of the calendar and raised OverflowError as a raw traceback
        for days in (parse_age("3000y"), parse_age("99999999"), 10 ** 12):
            with self.subTest(days=days):
                self.assertEqual(imap_date(days, today=datetime(2026, 7, 2)), "01-Jan-0001")

    def test_the_clamped_date_is_still_a_4_digit_year(self):
        # IMAP's date-year is 4DIGIT, so "01-Jan-1" would be a malformed search
        self.assertRegex(imap_date(10 ** 9, today=datetime(2026, 7, 2)), r"^\d{2}-\w{3}-\d{4}$")

    def test_an_absurd_age_reaches_the_search_criteria(self):
        crit = build_standard_criteria(Filters(older_than_days=parse_age("3000y")))
        self.assertIn("BEFORE", crit)
        self.assertIn("01-Jan-0001", crit)


class TestGmailQuery(unittest.TestCase):
    def test_default_filters(self):
        q = build_gmail_query(Filters())
        self.assertIn("category:promotions", q)
        self.assertIn("older_than:30d", q)
        self.assertIn("-is:starred", q)
        self.assertIn("-is:important", q)

    def test_protected_senders_are_excluded(self):
        q = build_gmail_query(Filters(protected_senders=["amazon.com", "boss@work.com"]))
        self.assertIn("-from:amazon.com", q)
        self.assertIn("-from:boss@work.com", q)

    def test_multiple_categories_use_or(self):
        q = build_gmail_query(Filters(categories=["promotions", "social"]))
        self.assertIn("category:promotions OR category:social", q)

    def test_zero_age_omits_older_than(self):
        q = build_gmail_query(Filters(older_than_days=0))
        self.assertNotIn("older_than", q)

    def test_include_starred_removes_guards(self):
        q = build_gmail_query(Filters(include_starred=True))
        self.assertNotIn("-is:starred", q)

    def test_invalid_categories_raise(self):
        with self.assertRaises(CleanerError):
            build_gmail_query(Filters(categories=["spam-folder"]))

    def test_keywords_are_ored(self):
        q = build_gmail_query(Filters(keywords=["sale", "newsletter"]))
        self.assertIn("(sale OR newsletter)", q)

    def test_keyword_phrases_get_quoted(self):
        q = build_gmail_query(Filters(keywords=["50% off"]))
        self.assertIn('"50% off"', q)

    def test_from_senders(self):
        q = build_gmail_query(Filters(from_senders=["doordash.com", "uber.com"]))
        self.assertIn("(from:doordash.com OR from:uber.com)", q)

    def test_all_skips_category(self):
        q = build_gmail_query(Filters(promo_only=False))
        self.assertNotIn("category:", q)
        self.assertIn("older_than:30d", q)

    def test_all_with_nothing_else_is_empty(self):
        q = build_gmail_query(
            Filters(promo_only=False, older_than_days=0, include_starred=True)
        )
        self.assertEqual(q, "")


class TestStandardCriteria(unittest.TestCase):
    def test_includes_unsubscribe_header_and_date(self):
        crit = build_standard_criteria(Filters(older_than_days=30))
        self.assertEqual(crit[:3], ["HEADER", "List-Unsubscribe", '""'])
        self.assertIn("BEFORE", crit)
        self.assertIn("UNFLAGGED", crit)

    def test_zero_age_has_no_before(self):
        crit = build_standard_criteria(Filters(older_than_days=0))
        self.assertNotIn("BEFORE", crit)

    def test_single_keyword(self):
        crit = build_standard_criteria(Filters(keywords=["sale"]))
        self.assertIn("TEXT", crit)
        self.assertIn('"sale"', crit)
        self.assertNotIn("OR", crit)

    def test_multiple_keywords_use_prefix_or(self):
        crit = build_standard_criteria(Filters(keywords=["a", "b", "c"]))
        # two ORs for three terms, stacked in front
        idx = crit.index("OR")
        self.assertEqual(crit[idx : idx + 2], ["OR", "OR"])
        self.assertEqual(crit.count("TEXT"), 3)

    def test_from_senders(self):
        crit = build_standard_criteria(Filters(from_senders=["doordash.com"]))
        self.assertIn("FROM", crit)
        self.assertIn('"doordash.com"', crit)

    def test_all_drops_unsubscribe_requirement(self):
        crit = build_standard_criteria(Filters(promo_only=False))
        self.assertNotIn("List-Unsubscribe", crit)

    def test_no_filters_at_all_searches_all(self):
        crit = build_standard_criteria(
            Filters(promo_only=False, older_than_days=0, include_starred=True)
        )
        self.assertEqual(crit, ["ALL"])


class TestBlankFilterTerms(unittest.TestCase):
    """A blank term used to widen the search instead of doing nothing: IMAP's
    TEXT "" is in every message, so one empty --keyword matched the whole
    folder. Padding did the same on Gmail, where '-from: x' is an empty
    exclusion plus a required keyword."""

    def test_blank_keyword_does_not_become_match_everything(self):
        crit = build_standard_criteria(
            Filters(keywords=[""], promo_only=False, older_than_days=0, include_starred=True)
        )
        self.assertNotIn("TEXT", crit)
        self.assertEqual(crit, ["ALL"])

    def test_blank_terms_are_dropped_but_real_ones_survive(self):
        crit = build_standard_criteria(
            Filters(keywords=["", "sale", "   "], promo_only=False,
                    older_than_days=0, include_starred=True)
        )
        self.assertEqual(crit.count("TEXT"), 1)
        self.assertIn('"sale"', crit)
        self.assertNotIn("OR", crit)

    def test_blank_sender_is_dropped(self):
        crit = build_standard_criteria(
            Filters(from_senders=["  "], promo_only=False,
                    older_than_days=0, include_starred=True)
        )
        self.assertEqual(crit, ["ALL"])

    def test_padded_terms_are_stripped_before_searching(self):
        crit = build_standard_criteria(Filters(keywords=[" sale "]))
        self.assertIn('"sale"', crit)
        self.assertNotIn('" sale "', crit)

    def test_padded_protect_does_not_become_a_required_keyword(self):
        q = build_gmail_query(Filters(protected_senders=[" amazon.com"]))
        self.assertIn("-from:amazon.com", q)
        self.assertNotIn("-from: ", q)

    def test_blank_protect_leaves_no_dangling_exclusion(self):
        q = build_gmail_query(Filters(protected_senders=["", "  "]))
        self.assertNotIn("-from:", q)

    def test_protect_with_a_space_is_quoted(self):
        q = build_gmail_query(Filters(protected_senders=["big corp"]))
        self.assertIn('-from:"big corp"', q)

    def test_blank_keyword_does_not_hide_the_wide_open_warning(self):
        # cli decides "this matches everything" from these lists, and a [""]
        # list is truthy - so the warning was skipped on the widest search there is
        filters = Filters(keywords=[""], from_senders=[""], promo_only=False,
                          older_than_days=0, include_starred=True)
        self.assertEqual(filters.keywords, [])
        self.assertEqual(filters.from_senders, [])


class _PickyServer:
    """A server that refuses HEADER searches, the way Yahoo does, and drops
    LIST-UNSUBSCRIBE from a HEADER.FIELDS fetch while still holding it."""

    supports_gmail_search = False

    def __init__(self, mail, refuse_header=True, hide_unsub_in_fields=True):
        self.mail = mail
        self.refuse_header = refuse_header
        self.hide_unsub_in_fields = hide_unsub_in_fields
        self.searches = []
        self.full_header_fetches = []

    def search_standard(self, criteria):
        self.searches.append(list(criteria))
        if self.refuse_header and "HEADER" in criteria:
            raise SearchUnsupported("the server rejected this search (BAD)")
        return [m.uid for m in self.mail]

    def fetch_summaries(self, uids, on_progress=None, full_headers=False):
        self.full_header_fetches.append(full_headers)
        out = []
        for m in self.mail:
            if m.uid not in uids:
                continue
            if not full_headers and self.hide_unsub_in_fields:
                m = dataclasses.replace(m, unsubscribe=[])
            out.append(m)
        return out


def _promo(uid, unsub=True):
    return _mail(uid, unsub=["https://x.com/u"] if unsub else [])


class TestHeaderSearchFallback(unittest.TestCase):
    """Yahoo answers any HEADER search with BAD, and that test is the whole of
    the promotional rule off Gmail. Rather than fail the run, scan() searches
    on what the server does accept and applies List-Unsubscribe on our side."""

    def test_criteria_can_be_built_without_the_header_term(self):
        crit = build_standard_criteria(Filters(), promo_via_header=False)
        self.assertNotIn("HEADER", crit)
        self.assertNotIn("List-Unsubscribe", crit)
        self.assertIn("BEFORE", crit)

    def test_falls_back_and_filters_on_our_side(self):
        server = _PickyServer([_promo("1"), _promo("2", unsub=False), _promo("3")])
        res = scan(server, Filters())
        self.assertEqual([e.uid for e in res.emails], ["1", "3"])
        self.assertEqual(res.skipped_not_promo, 1)
        # first attempt used HEADER, the retry did not
        self.assertIn("HEADER", server.searches[0])
        self.assertNotIn("HEADER", server.searches[1])

    def test_the_fallback_asks_for_the_whole_header_block(self):
        # the narrow field list is exactly what comes back without the header
        server = _PickyServer([_promo("1")])
        res = scan(server, Filters())
        self.assertEqual(server.full_header_fetches, [True])
        self.assertEqual([e.uid for e in res.emails], ["1"])

    def test_a_working_server_keeps_the_narrow_fetch(self):
        server = _PickyServer([_promo("1")], refuse_header=False)
        scan(server, Filters())
        self.assertEqual(server.searches, [build_standard_criteria(Filters())])
        self.assertEqual(server.full_header_fetches, [False])

    def test_limit_counts_matches_not_candidates(self):
        # the server hands back everything old, so capping before the promo
        # filter would give fewer than --limit asked for
        mail = [_promo("1", unsub=False), _promo("2"), _promo("3", unsub=False), _promo("4")]
        res = scan(_PickyServer(mail), Filters(limit=2))
        self.assertEqual([e.uid for e in res.emails], ["2", "4"])

    def test_protected_senders_still_win_on_the_fallback(self):
        mail = [_promo("1"), _promo("2")]
        mail[0] = dataclasses.replace(mail[0], sender_email="deals@amazon.com")
        res = scan(_PickyServer(mail), Filters(protected_senders=["amazon.com"]))
        self.assertEqual([e.uid for e in res.emails], ["2"])
        self.assertEqual(res.skipped_protected, 1)

    def test_all_has_nothing_to_fall_back_to(self):
        # --all sends no HEADER term, so a refusal here is a real failure
        server = _PickyServer([_promo("1")])
        server.search_standard = lambda c: (_ for _ in ()).throw(
            SearchUnsupported("nope")
        )
        with self.assertRaises(SearchUnsupported):
            scan(server, Filters(promo_only=False))

    def test_the_description_says_where_the_filtering_happened(self):
        res = scan(_PickyServer([_promo("1")]), Filters())
        self.assertIn("unsubscribe header checked on this machine", res.query_description)


class TestSearchErrorsAreCatchable(unittest.TestCase):
    """imaplib raises on a BAD reply before the status reaches _search_result,
    so a refused search used to surface as a traceback."""

    def _session(self, exc):
        session, conn = _fake_session()
        conn.uid = mock.Mock(side_effect=exc)
        return session

    def test_bad_becomes_search_unsupported(self):
        session = self._session(imaplib.IMAP4.error("BAD [CANNOT] not supported"))
        with self.assertRaises(SearchUnsupported):
            session.search_standard(["HEADER", "List-Unsubscribe", chr(34) * 2])

    def test_search_unsupported_still_prints_like_any_other_error(self):
        self.assertTrue(issubclass(SearchUnsupported, CleanerError))

    def test_a_dropped_connection_is_not_mistaken_for_a_refusal(self):
        session = self._session(imaplib.IMAP4.abort("socket error"))
        with self.assertRaises(CleanerError) as caught:
            session.search_standard(["ALL"])
        self.assertNotIsInstance(caught.exception, SearchUnsupported)
        self.assertIn("dropped", str(caught.exception))

    def test_gmail_search_is_guarded_too(self):
        session = self._session(imaplib.IMAP4.error("BAD"))
        with self.assertRaises(SearchUnsupported):
            session.search_gmail_raw("category:promotions")


class TestProtection(unittest.TestCase):
    def _summary(self, sender):
        return EmailSummary(
            uid="1", sender_name="", sender_email=sender, subject="s",
            date="2026-01-01", size=100, flagged=False,
        )

    def test_domain_match(self):
        self.assertTrue(is_protected(self._summary("deals@amazon.com"), ["amazon.com"]))

    def test_exact_address_match(self):
        self.assertTrue(is_protected(self._summary("boss@work.com"), ["boss@work.com"]))

    def test_case_insensitive(self):
        self.assertTrue(is_protected(self._summary("deals@amazon.com"), ["AMAZON.COM"]))

    def test_no_match(self):
        self.assertFalse(is_protected(self._summary("deals@shop.com"), ["amazon.com"]))

    def test_blank_patterns_ignored(self):
        self.assertFalse(is_protected(self._summary("a@b.com"), ["", "  "]))

    def test_whitespace_padded_pattern_still_protects(self):
        # a stray space around a protect entry must not silently disable it
        self.assertTrue(is_protected(self._summary("deals@amazon.com"), [" amazon.com"]))
        self.assertTrue(is_protected(self._summary("deals@amazon.com"), ["amazon.com  "]))


class TestApplyLimit(unittest.TestCase):
    def test_none_keeps_everything(self):
        self.assertEqual(_apply_limit(["1", "2", "3"], None), ["1", "2", "3"])

    def test_zero_keeps_nothing(self):
        # --limit 0 means none, not "no limit"
        self.assertEqual(_apply_limit(["1", "2", "3"], 0), [])

    def test_trims_to_the_oldest(self):
        self.assertEqual(_apply_limit(["1", "2", "3"], 2), ["1", "2"])

    def test_limit_above_count_keeps_everything(self):
        self.assertEqual(_apply_limit(["1", "2", "3"], 9), ["1", "2", "3"])

    def test_negative_clamps_to_none_kept(self):
        self.assertEqual(_apply_limit(["1", "2", "3"], -5), [])


class TestUnsubscribeParsing(unittest.TestCase):
    def test_https_preferred_over_mailto(self):
        urls = extract_unsubscribe_urls("<mailto:u@x.com>, <https://x.com/unsub>")
        self.assertEqual(urls[0], "https://x.com/unsub")

    def test_empty_header(self):
        self.assertEqual(extract_unsubscribe_urls(""), [])


class TestImapHelpers(unittest.TestCase):
    def test_quote_escapes_quotes_and_backslashes(self):
        self.assertEqual(quote_imap_string('a"b\\c'), '"a\\"b\\\\c"')

    def test_decode_mime_header(self):
        self.assertEqual(decode_mime_header("=?UTF-8?B?SMOpbGxv?="), "Héllo")

    def test_decode_plain_header_passthrough(self):
        self.assertEqual(decode_mime_header("Plain text"), "Plain text")

    def test_parse_fetch_response(self):
        headers = (
            b"From: Shop <deals@shop.com>\r\n"
            b"Subject: =?UTF-8?B?U2FsZSE=?=\r\n"
            b"Date: Tue, 02 Jun 2026 10:00:00 +0000\r\n"
            b"List-Unsubscribe: <https://shop.com/unsub>\r\n\r\n"
        )
        data = [
            (b"1 (UID 4321 RFC822.SIZE 2048 FLAGS (\\Seen) BODY[HEADER.FIELDS "
             b"(FROM SUBJECT DATE LIST-UNSUBSCRIBE)] {%d}" % len(headers), headers),
            b")",
        ]
        (summary,) = _parse_fetch_response(data)
        self.assertEqual(summary.uid, "4321")
        self.assertEqual(summary.size, 2048)
        self.assertEqual(summary.sender_email, "deals@shop.com")
        self.assertEqual(summary.subject, "Sale!")
        self.assertEqual(summary.date, "2026-06-02")
        self.assertFalse(summary.flagged)
        self.assertEqual(summary.unsubscribe, ["https://shop.com/unsub"])

    def test_parse_fetch_flagged(self):
        data = [(b"2 (UID 7 RFC822.SIZE 10 FLAGS (\\Flagged \\Seen) BODY[X] {2}", b"\r\n")]
        (summary,) = _parse_fetch_response(data)
        self.assertTrue(summary.flagged)


class _FakeConn:
    """Records the commands an ImapSession issues, so the delete and search
    paths can be checked without a server."""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = fail_on
        self.state = "SELECTED"

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command in self.fail_on:
            return "NO", [b"server said no"]
        return "OK", [None]

    def expunge(self):
        self.calls.append(("EXPUNGE",))
        return "OK", [None]

    def status(self, mailbox, names):
        self.calls.append(("STATUS", mailbox, names))
        return "NO", [None]

    def select(self, mailbox, readonly=False):
        self.calls.append(("SELECT", mailbox, readonly))
        self.state = "SELECTED"
        return "OK", [b"7"]

    def close(self):
        self.calls.append(("CLOSE",))
        self.state = "AUTH"
        return "OK", [None]

    def unselect(self):
        self.calls.append(("UNSELECT",))
        self.state = "AUTH"
        return "OK", [None]

    def logout(self):
        self.calls.append(("LOGOUT",))
        self.state = "LOGOUT"
        return "OK", [None]


def _fake_session(capabilities=(), fail_on=()):
    session = ImapSession("host", 993, "me@x.com", "pw")
    session._capabilities = {c.upper() for c in capabilities}
    session._imap = _FakeConn(fail_on)
    session.selected_folder = "INBOX"
    return session, session._imap


class TestExpungeScope(unittest.TestCase):
    """A bare EXPUNGE purges every \\Deleted message in the open folder, not
    just the ones we flagged, so it can destroy mail another client had marked
    and never compacted. UID EXPUNGE keeps us to our own UIDs."""

    def test_permanent_delete_names_its_own_uids(self):
        session, conn = _fake_session(["UIDPLUS"])
        session.delete_permanently(["1", "2"])
        self.assertIn(("EXPUNGE", "1,2"), conn.calls)
        self.assertNotIn(("EXPUNGE",), conn.calls)
        self.assertIsNone(session.expunge_notice)

    def test_server_without_uidplus_falls_back_and_warns(self):
        session, conn = _fake_session()
        session.delete_permanently(["1"])
        self.assertIn(("EXPUNGE",), conn.calls)
        self.assertIsNotNone(session.expunge_notice)

    def test_empty_match_set_purges_nothing(self):
        # an empty result used to still fire a folder-wide EXPUNGE
        session, conn = _fake_session()
        self.assertEqual(session.delete_permanently([]), 0)
        self.assertEqual(conn.calls, [])

    def test_copy_fallback_purges_only_what_it_copied(self):
        session, conn = _fake_session(["UIDPLUS"])  # no MOVE extension
        session.move_to_trash(["7", "8"], "Trash")
        self.assertIn(("COPY", "7,8", '"Trash"'), conn.calls)
        self.assertIn(("EXPUNGE", "7,8"), conn.calls)
        self.assertNotIn(("EXPUNGE",), conn.calls)

    def test_move_extension_never_expunges(self):
        session, conn = _fake_session(["MOVE", "UIDPLUS"])
        session.move_to_trash(["7"], "Trash")
        self.assertEqual(conn.calls, [("MOVE", "7", '"Trash"')])

    def test_failed_store_after_a_copy_raises(self):
        # the copy already landed, so carrying on would leave it in both places
        session, _ = _fake_session(["UIDPLUS"], fail_on=("STORE",))
        with self.assertRaises(CleanerError):
            session.move_to_trash(["7"], "Trash")


class TestCapabilities(unittest.TestCase):
    """The post-login refresh is only ever added to what imaplib parsed at
    login. Replacing it meant a NO or empty CAPABILITY reply left the session
    believing the server supports nothing - which silently swaps Gmail search
    for a different match set and drops UID EXPUNGE for the unscoped one."""

    class _Conn:
        capabilities = ("IMAP4REV1", "MOVE", "UIDPLUS")

        def __init__(self, reply):
            self.reply = reply

        def capability(self):
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    def _caps(self, reply):
        session = ImapSession("host", 993, "me@x.com", "pw")
        session._imap = self._Conn(reply)
        session._load_capabilities()
        return session

    def test_refresh_adds_what_login_did_not_advertise(self):
        session = self._caps(("OK", [b"IMAP4REV1 MOVE UIDPLUS X-GM-EXT-1 UNSELECT"]))
        self.assertTrue(session.supports_gmail_search)
        self.assertTrue(session.supports_unselect)
        self.assertTrue(session.supports_uid_expunge)

    def test_a_useless_refresh_keeps_the_login_capabilities(self):
        for reply in (
            ("NO", [b"not now"]),
            ("OK", [b""]),
            ("OK", [None]),
            ("OK", []),
            imaplib.IMAP4.error("boom"),
        ):
            with self.subTest(reply=reply):
                session = self._caps(reply)
                self.assertTrue(session.supports_uid_expunge, "lost scoped expunge")
                self.assertTrue(session.supports_move, "lost MOVE")

    def test_a_server_that_really_has_nothing_is_believed(self):
        session = ImapSession("host", 993, "me@x.com", "pw")
        session._imap = self._Conn(("OK", [b"IMAP4REV1"]))
        session._imap.capabilities = ("IMAP4REV1",)
        session._load_capabilities()
        self.assertFalse(session.supports_uid_expunge)
        self.assertFalse(session.supports_gmail_search)


class TestConnectErrors(unittest.TestCase):
    """ssl.SSLError subclasses OSError, so catching the latter first swallows
    every TLS failure and blames the network for a wrong port or a bad cert."""

    def _connect_with(self, exc):
        session = ImapSession("mail.example.com", 143, "me@x.com", "pw")
        with mock.patch("imaplib.IMAP4_SSL", side_effect=exc):
            with self.assertRaises(CleanerError) as caught:
                session.connect()
        return caught.exception

    def test_tls_failure_is_reported_as_a_tls_failure(self):
        err = self._connect_with(ssl.SSLError("WRONG_VERSION_NUMBER"))
        self.assertIn("TLS handshake", str(err))
        self.assertIn("993", err.hint)

    def test_a_bad_certificate_is_a_tls_failure_too(self):
        err = self._connect_with(ssl.SSLCertVerificationError("certificate verify failed"))
        self.assertIn("TLS handshake", str(err))

    def test_an_unreachable_host_still_reports_reachability(self):
        for exc in (
            socket.gaierror("name or service not known"),
            TimeoutError("timed out"),
            ConnectionRefusedError("refused"),
        ):
            with self.subTest(exc=type(exc).__name__):
                err = self._connect_with(exc)
                self.assertIn("Could not reach", str(err))
                self.assertNotIn("TLS", str(err))

    def test_a_rejected_password_is_still_a_login_error(self):
        session = ImapSession("mail.example.com", 993, "me@x.com", "pw")
        conn = mock.Mock()
        conn.login.side_effect = imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        with mock.patch("imaplib.IMAP4_SSL", return_value=conn):
            with self.assertRaises(CleanerError) as caught:
                session.connect()
        self.assertIn("Login failed", str(caught.exception))


class TestSessionTeardown(unittest.TestCase):
    """IMAP CLOSE purges every \\Deleted message in a writable mailbox on its
    way out - the same unscoped wipe UID EXPUNGE exists to avoid. 'clean' opens
    the folder read-write, so hanging up that way silently destroyed mail
    another client had flagged and never compacted."""

    def test_teardown_never_sends_close(self):
        session, conn = _fake_session(["UIDPLUS"])
        session.close()
        self.assertNotIn(("CLOSE",), conn.calls)
        self.assertIn(("LOGOUT",), conn.calls)

    def test_unselect_is_used_when_the_server_offers_it(self):
        session, conn = _fake_session(["UIDPLUS", "UNSELECT"])
        session.close()
        self.assertEqual(conn.calls, [("UNSELECT",), ("LOGOUT",)])

    def test_a_clean_run_purges_only_its_own_uids(self):
        # end to end: flag, scoped purge, hang up - and nothing else
        session, conn = _fake_session(["UIDPLUS", "UNSELECT"])
        session.delete_permanently(["4", "5"])
        session.close()
        self.assertEqual(
            conn.calls,
            [
                ("STORE", "4,5", "+FLAGS.SILENT", "(\\Deleted)"),
                ("EXPUNGE", "4,5"),
                ("UNSELECT",),
                ("LOGOUT",),
            ],
        )

    def test_teardown_is_still_safe_without_unselect(self):
        session, conn = _fake_session()
        session.close()
        self.assertEqual(conn.calls, [("LOGOUT",)])

    def test_closing_twice_is_a_noop(self):
        session, conn = _fake_session(["UNSELECT"])
        session.close()
        session.close()
        self.assertEqual(conn.calls.count(("LOGOUT",)), 1)


class TestNonAsciiSearch(unittest.TestCase):
    """imaplib encodes command arguments as ascii, so an accented keyword blew
    up with UnicodeEncodeError before the search was ever sent."""

    def test_ascii_criteria_are_left_alone(self):
        self.assertEqual(search_args(["FROM", '"a@b.com"']), ["FROM", '"a@b.com"'])

    def test_non_ascii_declares_utf8_and_pre_encodes(self):
        args = search_args(["TEXT", '"café"'])
        self.assertEqual(args[:2], ["CHARSET", "UTF-8"])
        self.assertEqual(args[2:], [b"TEXT", '"café"'.encode("utf-8")])

    def test_every_argument_survives_what_imaplib_does_to_it(self):
        for arg in search_args(["TEXT", '"日本語"']):
            if not isinstance(arg, bytes):
                arg.encode("ascii")  # imaplib does this; must not raise

    def test_standard_search_sends_the_charset(self):
        session, conn = _fake_session()
        session.search_standard(["TEXT", '"café"'])
        self.assertEqual(conn.calls[0][:3], ("SEARCH", "CHARSET", "UTF-8"))

    def test_gmail_raw_search_sends_the_charset(self):
        session, conn = _fake_session(["X-GM-EXT-1"])
        session.search_gmail_raw("category:promotions Grüße")
        self.assertIn("CHARSET", conn.calls[0])


class TestMailboxNames(unittest.TestCase):
    def test_ascii_passes_through(self):
        self.assertEqual(encode_mailbox_name("[Gmail]/Trash"), "[Gmail]/Trash")

    def test_non_ascii_becomes_modified_utf7(self):
        self.assertEqual(encode_mailbox_name("Gelöscht"), "Gel&APY-scht")

    def test_an_already_encoded_name_is_not_encoded_twice(self):
        # LIST hands names back in this encoding already; escaping the '&' a
        # second time would point us at a folder that does not exist
        self.assertEqual(
            encode_mailbox_name("INBOX.Gel&APY-scht"), "INBOX.Gel&APY-scht"
        )

    def test_quoted_mailbox_is_ascii_safe(self):
        quoted = quoted_mailbox("Élements supprimés")
        quoted.encode("ascii")  # must not raise
        self.assertEqual(quoted, '"&AMk-lements supprim&AOk-s"')


class TestUiHelpers(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(human_size(48 * 1024 * 1024), "48.0 MB")

    def test_human_size_rolls_over_at_boundary(self):
        # 1048544 is 1023.97 KB; it must read as 1.0 MB, not "1024.0 KB"
        self.assertEqual(human_size(1048544), "1.0 MB")
        self.assertEqual(human_size(1024 * 1024), "1.0 MB")

    def test_truncate(self):
        self.assertEqual(truncate("hello", 10), "hello")
        self.assertEqual(truncate("hello world", 8), "hello...")
        self.assertEqual(truncate("hello", 2), "he")
        self.assertEqual(truncate("hi", 0), "")

    def test_survives_a_missing_stdout(self):
        # under pythonw sys.stdout is None; colors_enabled runs at import and
        # called .isatty() on it, so importing ui was enough to crash
        with mock.patch.object(sys, "stdout", None):
            self.assertFalse(colors_enabled())
            progress(1, 2, "Reading")  # must not raise either


class TestSummaries(unittest.TestCase):
    def test_summarize_senders_ranks_by_count(self):
        def mail(sender, size):
            return EmailSummary(
                uid="1", sender_name="", sender_email=sender, subject="s",
                date="", size=size, flagged=False,
            )

        emails = [mail("a@x.com", 10), mail("a@x.com", 20), mail("b@y.com", 99)]
        ranked = summarize_senders(emails)
        self.assertEqual(ranked[0], ("a@x.com", 2, 30))
        self.assertEqual(ranked[1], ("b@y.com", 1, 99))


class TestProviders(unittest.TestCase):
    def test_guess_gmail(self):
        self.assertEqual(guess_provider("me@gmail.com").key, "gmail")

    def test_guess_unknown_is_none(self):
        self.assertIsNone(guess_provider("me@mycompany.io"))

    def test_get_provider_bad_key(self):
        with self.assertRaises(ValueError):
            get_provider("aol")


class TestDotenv(unittest.TestCase):
    def setUp(self):
        # stash and clear the keys we poke at, restore them in tearDown
        self._saved = {k: os.environ.get(k) for k in
                       ("EMAIL_CLEANER_EMAIL", "EMAIL_CLEANER_PASSWORD", "EMAIL_CLEANER_HOST")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write(self, text):
        d = tempfile.mkdtemp()
        p = Path(d) / ".env"
        p.write_text(text, encoding="utf-8")
        return p

    def test_reads_keys(self):
        config.load_dotenv(self._write("EMAIL_CLEANER_EMAIL=me@gmail.com\n"))
        self.assertEqual(os.environ["EMAIL_CLEANER_EMAIL"], "me@gmail.com")

    def test_ignores_comments_and_blanks(self):
        config.load_dotenv(self._write("# nope\n\nEMAIL_CLEANER_HOST=h.example.com\n"))
        self.assertEqual(os.environ["EMAIL_CLEANER_HOST"], "h.example.com")

    def test_strips_quotes_and_export(self):
        config.load_dotenv(self._write('export EMAIL_CLEANER_PASSWORD="a b c"\n'))
        self.assertEqual(os.environ["EMAIL_CLEANER_PASSWORD"], "a b c")

    def test_strips_leading_bom(self):
        # Notepad / PowerShell write a UTF-8 BOM; the first key must still parse
        config.load_dotenv(self._write("﻿EMAIL_CLEANER_EMAIL=me@gmail.com\n"))
        self.assertEqual(os.environ["EMAIL_CLEANER_EMAIL"], "me@gmail.com")

    def test_strips_inline_comment(self):
        config.load_dotenv(self._write("EMAIL_CLEANER_HOST=h.example.com  # main server\n"))
        self.assertEqual(os.environ["EMAIL_CLEANER_HOST"], "h.example.com")

    def test_hash_inside_quoted_value_is_kept(self):
        config.load_dotenv(self._write('EMAIL_CLEANER_PASSWORD="a # b"\n'))
        self.assertEqual(os.environ["EMAIL_CLEANER_PASSWORD"], "a # b")

    def test_quoted_value_with_a_trailing_comment_loses_its_quotes(self):
        # the quotes used to survive into the value, because the line no longer
        # *ended* on one - so IMAP got a password with two stray '"' in it and
        # the user was told their app password was wrong
        config.load_dotenv(
            self._write('EMAIL_CLEANER_PASSWORD="abcd efgh"  # gmail app password\n')
        )
        self.assertEqual(os.environ["EMAIL_CLEANER_PASSWORD"], "abcd efgh")

    def test_single_quoted_value_with_a_trailing_comment(self):
        config.load_dotenv(self._write("EMAIL_CLEANER_HOST='h.example.com'  # main\n"))
        self.assertEqual(os.environ["EMAIL_CLEANER_HOST"], "h.example.com")

    def test_a_hash_after_the_closing_quote_is_a_comment_one_inside_is_not(self):
        config.load_dotenv(self._write('EMAIL_CLEANER_PASSWORD="a # b"  # note\n'))
        self.assertEqual(os.environ["EMAIL_CLEANER_PASSWORD"], "a # b")

    def test_unterminated_quote_is_left_alone(self):
        config.load_dotenv(self._write('EMAIL_CLEANER_HOST="h.example.com\n'))
        self.assertEqual(os.environ["EMAIL_CLEANER_HOST"], '"h.example.com')

    def test_real_env_is_not_overwritten(self):
        os.environ["EMAIL_CLEANER_EMAIL"] = "real@env.com"
        config.load_dotenv(self._write("EMAIL_CLEANER_EMAIL=file@env.com\n"))
        self.assertEqual(os.environ["EMAIL_CLEANER_EMAIL"], "real@env.com")

    def test_missing_file_is_a_noop(self):
        config.load_dotenv(Path(tempfile.mkdtemp()) / "does-not-exist.env")  # no raise


class TestResolveAccount(unittest.TestCase):
    """resolve_account reads the environment, so stash/restore the keys."""

    KEYS = ("EMAIL_CLEANER_EMAIL", "EMAIL_CLEANER_PASSWORD",
            "EMAIL_CLEANER_HOST", "EMAIL_CLEANER_PORT")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _args(self, **over):
        # stand-in for the argparse Namespace; only the attrs it reads
        blank = dict(email="me@gmail.com", provider=None, host=None,
                     port=None, trash_folder=None)
        blank.update(over)
        return type("Args", (), blank)()

    def test_bad_port_raises_friendly_error(self):
        os.environ["EMAIL_CLEANER_PASSWORD"] = "x"  # so it doesn't try to prompt
        os.environ["EMAIL_CLEANER_PORT"] = "not-a-number"
        with self.assertRaises(CleanerError):
            config.resolve_account(self._args())

    def test_unknown_provider_is_a_friendly_error_not_a_traceback(self):
        # --provider has no argparse choices, so a typo reached the user as a
        # raw ValueError traceback and exit 1 instead of the documented exit 2
        os.environ["EMAIL_CLEANER_PASSWORD"] = "x"
        with self.assertRaises(CleanerError) as caught:
            config.resolve_account(self._args(provider="fastmail"))
        self.assertIn("fastmail", str(caught.exception))
        self.assertTrue(caught.exception.hint)

    def test_known_provider_still_resolves(self):
        os.environ["EMAIL_CLEANER_PASSWORD"] = "x"
        account = config.resolve_account(self._args(provider="GMAIL"))
        self.assertEqual(account.host, "imap.gmail.com")


def _settings(backend="ollama", **over):
    base = dict(
        backend=backend,
        model="m",
        host={"ollama": "http://localhost:11434",
              "openai": "https://api.openai.com/v1",
              "anthropic": "https://api.anthropic.com"}[backend],
        api_key="k" if backend != "ollama" else None,
        prompt="delete marketing, keep receipts",
        snippet=False,
    )
    base.update(over)
    return AISettings(**base)


class TestAiSettings(unittest.TestCase):
    def test_ollama_is_local(self):
        s = _settings("ollama")
        self.assertTrue(s.is_local)
        self.assertFalse(s.is_hosted)

    def test_hosted_backends_leave_the_machine(self):
        self.assertTrue(_settings("openai").is_hosted)
        self.assertTrue(_settings("anthropic").is_hosted)

    def test_provider_host_is_just_the_hostname(self):
        self.assertEqual(_settings("anthropic").provider_host, "api.anthropic.com")
        self.assertEqual(_settings("openai").provider_host, "api.openai.com")

    def test_openai_pointed_at_localhost_counts_as_local(self):
        # someone using ollama's openai-compatible endpoint sends nothing off-box
        s = _settings("openai", host="http://localhost:11434/v1")
        self.assertTrue(s.is_local)

    def test_loopback_ip_counts_as_local(self):
        self.assertTrue(_settings("ollama", host="http://127.0.0.1:11434").is_local)

    def test_host_we_cannot_read_counts_as_hosted(self):
        # 'api.openai.com/v1' has no scheme, so urlparse finds no hostname at
        # all. Reading that as local skipped the privacy warning and the
        # consent prompt for a very much remote endpoint.
        self.assertTrue(_settings("openai", host="api.openai.com/v1").is_hosted)
        self.assertTrue(_settings("ollama", host="").is_hosted)

    def test_a_box_on_the_lan_is_not_this_machine(self):
        self.assertTrue(_settings("ollama", host="http://192.168.1.5:11434").is_hosted)


class TestLenientJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(_loads_lenient('{"a": 1}'), {"a": 1})

    def test_wrapped_in_prose_and_fences(self):
        raw = 'Sure!\n```json\n{"results": []}\n```\nHope that helps'
        self.assertEqual(_loads_lenient(raw), {"results": []})

    def test_bare_array(self):
        self.assertEqual(_loads_lenient("[1, 2, 3]"), [1, 2, 3])

    def test_wrapped_single_element_array_is_not_grabbed_as_its_object(self):
        # a fenced one-item array must parse as the LIST, not its inner object,
        # or _parse_verdicts drops the verdict and the message is wrongly kept
        raw = 'Here:\n```json\n[{"uid": "1", "action": "delete"}]\n```'
        self.assertEqual(_loads_lenient(raw), [{"uid": "1", "action": "delete"}])

    def test_wrapped_object_still_wins_when_it_starts_first(self):
        raw = 'Sure:\n```json\n{"results": [{"uid": "1"}]}\n```'
        self.assertEqual(_loads_lenient(raw), {"results": [{"uid": "1"}]})

    def test_garbage_is_none(self):
        self.assertIsNone(_loads_lenient("not json at all"))
        self.assertIsNone(_loads_lenient(""))


class TestParseVerdicts(unittest.TestCase):
    def setUp(self):
        self.batch = [_mail("1"), _mail("2"), _mail("3")]

    def test_object_with_results(self):
        raw = json.dumps({"results": [
            {"uid": "1", "action": "delete", "reason": "promo"},
            {"uid": "2", "action": "keep", "reason": "receipt"},
        ]})
        out = _parse_verdicts(raw, self.batch)
        self.assertTrue(out["1"].delete)
        self.assertEqual(out["1"].reason, "promo")
        self.assertFalse(out["2"].delete)
        # uid 3 was not in the reply, so it is absent -> defaults to keep upstream
        self.assertNotIn("3", out)

    def test_bare_array_is_accepted(self):
        raw = json.dumps([{"uid": "1", "action": "delete"}])
        self.assertTrue(_parse_verdicts(raw, self.batch)["1"].delete)

    def test_fenced_single_element_array_verdict_survives(self):
        # regression: a prose/fence-wrapped one-item array used to be misparsed
        # as its inner object, silently dropping the verdict (message kept)
        raw = 'Result:\n```json\n[{"uid": "1", "action": "delete", "reason": "promo"}]\n```'
        out = _parse_verdicts(raw, self.batch)
        self.assertIn("1", out)
        self.assertTrue(out["1"].delete)
        self.assertEqual(out["1"].reason, "promo")

    def test_only_delete_deletes(self):
        raw = json.dumps({"results": [
            {"uid": "1", "action": "DELETE"},   # case-insensitive
            {"uid": "2", "action": "remove"},   # anything else keeps
        ]})
        out = _parse_verdicts(raw, self.batch)
        self.assertTrue(out["1"].delete)
        self.assertFalse(out["2"].delete)

    def test_unknown_uid_is_ignored(self):
        raw = json.dumps({"results": [{"uid": "999", "action": "delete"}]})
        self.assertEqual(_parse_verdicts(raw, self.batch), {})

    def test_malformed_reply_defaults_to_empty(self):
        # empty means every message keeps - the fail-safe
        self.assertEqual(_parse_verdicts("total nonsense", self.batch), {})
        self.assertEqual(_parse_verdicts('{"results": "oops"}', self.batch), {})


class TestClassifierRequests(unittest.TestCase):
    def _batch(self):
        return [_mail("1", subject="40% off"), _mail("2", subject="Your receipt")]

    def test_ollama_request(self):
        clf = Classifier(_settings("ollama"))
        url, headers, body = clf._build_request(self._batch(), {})
        self.assertEqual(url, "http://localhost:11434/api/chat")
        self.assertEqual(body["format"], "json")
        self.assertNotIn("Authorization", headers)
        self.assertIn("delete marketing", body["messages"][0]["content"])

    def test_openai_request_has_bearer_auth(self):
        clf = Classifier(_settings("openai", api_key="sk-abc"))
        url, headers, body = clf._build_request(self._batch(), {})
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-abc")
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_anthropic_request_uses_x_api_key(self):
        clf = Classifier(_settings("anthropic", api_key="ant-1", model="claude-haiku-4-5"))
        url, headers, body = clf._build_request(self._batch(), {})
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "ant-1")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["model"], "claude-haiku-4-5")
        self.assertIn("max_tokens", body)

    def test_snippet_is_included_in_the_prompt(self):
        clf = Classifier(_settings("ollama", snippet=True))
        _, _, body = clf._build_request(self._batch(), {"1": "limited time only"})
        self.assertIn("limited time only", body["messages"][1]["content"])


class TestClassifierClassify(unittest.TestCase):
    def _emails(self, n):
        return [_mail(str(i)) for i in range(n)]

    def test_batches_cover_every_message(self):
        clf = Classifier(_settings("ollama"), batch_size=10)
        seen_sizes = []

        def fake_call(batch, snippets):
            seen_sizes.append(len(batch))
            return json.dumps({"results": [
                {"uid": e.uid, "action": "delete", "reason": "x"} for e in batch
            ]})

        clf._call = fake_call
        out = clf.classify(self._emails(25))
        self.assertEqual(seen_sizes, [10, 10, 5])
        self.assertEqual(len(out), 25)
        self.assertTrue(all(v.delete for v in out.values()))

    def test_progress_reports_totals(self):
        clf = Classifier(_settings("ollama"), batch_size=10)
        clf._call = lambda batch, snippets: json.dumps({"results": []})
        seen = []
        clf.classify(self._emails(25), on_progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(10, 25), (20, 25), (25, 25)])

    def test_transport_failure_keeps_everything(self):
        clf = Classifier(_settings("openai", api_key="k"), batch_size=10)

        def boom(batch, snippets):
            raise _BackendError("connection refused")

        clf._call = boom
        out = clf.classify(self._emails(15))
        # empty verdicts means every message defaults to keep upstream
        self.assertEqual(out, {})
        self.assertIsNotNone(clf.transport_error)
        self.assertIn("openai", clf.transport_error)

    def test_one_bad_batch_does_not_sink_the_others(self):
        clf = Classifier(_settings("ollama"), batch_size=10)
        calls = {"n": 0}

        def flaky(batch, snippets):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _BackendError("timed out")
            return json.dumps({"results": [
                {"uid": e.uid, "action": "delete"} for e in batch
            ]})

        clf._call = flaky
        out = clf.classify(self._emails(15))  # 10 then 5
        # first batch failed (kept), second batch classified
        self.assertEqual(len(out), 5)


class TestExtractText(unittest.TestCase):
    """Every unreadable shape has to come back as None so _call turns it into a
    _BackendError and the batch keeps. A backend answering with a bare JSON
    array or string has no .get, and that AttributeError used to escape."""

    def test_normal_shapes(self):
        self.assertEqual(_extract_text("ollama", {"message": {"content": "hi"}}), "hi")
        self.assertEqual(
            _extract_text("openai", {"choices": [{"message": {"content": "hi"}}]}), "hi")
        self.assertEqual(
            _extract_text("anthropic", {"content": [{"type": "text", "text": "hi"}]}), "hi")

    def test_junk_payloads_are_none_not_exceptions(self):
        for backend in ("ollama", "openai", "anthropic"):
            for payload in ([], "oops", 7, None, {}, {"content": "not a list"}):
                with self.subTest(backend=backend, payload=payload):
                    self.assertIsNone(_extract_text(backend, payload))

    def test_a_junk_payload_keeps_the_batch_instead_of_crashing(self):
        # end to end through the real _call: valid JSON, wrong shape
        clf = Classifier(_settings("anthropic", api_key="k"), batch_size=10)

        class _Resp:
            def read(self):
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        with mock.patch("urllib.request.urlopen", lambda *a, **k: _Resp()):
            out = clf.classify([_mail("1"), _mail("2")])
        self.assertEqual(out, {})  # empty verdicts means keep, upstream
        self.assertIn("no content", clf.transport_error)


class TestApplyAi(unittest.TestCase):
    """scanner._apply_ai narrows the candidate set with the model's verdicts."""

    class _FakeClassifier:
        wants_snippet = False

        def __init__(self, verdicts):
            self._verdicts = verdicts

        def classify(self, candidates, snippets=None, on_progress=None):
            return self._verdicts

    def test_keeps_only_delete_verdicts(self):
        cands = [_mail("1"), _mail("2"), _mail("3")]
        clf = self._FakeClassifier({
            "1": Verdict(delete=True, reason="promo"),
            "2": Verdict(delete=False),
            # 3 missing -> unknown -> keep in mailbox
        })
        to_clean, skipped, reasons = _apply_ai(cands, clf, {}, None)
        self.assertEqual([e.uid for e in to_clean], ["1"])
        self.assertEqual(skipped, 2)
        self.assertEqual(reasons, {"1": "promo"})

    def test_empty_verdicts_delete_nothing(self):
        # models the fail-safe: an unreachable backend hands back {} and no
        # message is ever queued for deletion
        cands = [_mail("1"), _mail("2")]
        clf = self._FakeClassifier({})
        to_clean, skipped, reasons = _apply_ai(cands, clf, {}, None)
        self.assertEqual(to_clean, [])
        self.assertEqual(skipped, 2)
        self.assertEqual(reasons, {})


class TestSnippets(unittest.TestCase):
    def test_clean_strips_tags_and_collapses_whitespace(self):
        self.assertEqual(_clean_snippet(b"<p>Hello   world</p>\n"), "Hello world")

    def test_clean_handles_empty(self):
        self.assertEqual(_clean_snippet(b""), "")

    def test_parse_snippet_response(self):
        data = [
            (b"1 (UID 42 BODY[1]<0> {5}", b"hi yo"),
            b")",
        ]
        self.assertEqual(_parse_snippet_response(data), {"42": "hi yo"})


class TestMenuEmptyTrash(unittest.TestCase):
    """Saying yes in the menu is what makes cmd_clean skip its own confirmation,
    so it has to be the same full-word gate the --empty-trash flag gets. A plain
    [y/N] there meant one keystroke destroyed the entire Trash folder."""

    def _run_menu(self, answers=True):
        asked = []

        def fake_confirm(question, danger=False):
            asked.append((question, danger))
            return answers

        with mock.patch.object(cli.ui, "confirm", fake_confirm), \
                mock.patch.object(cli.ui, "prompt", lambda q, default=None: default or ""), \
                mock.patch.object(cli.ui, "heading", lambda *a, **k: None), \
                mock.patch("builtins.print", lambda *a, **k: None):
            args = cli.build_parser().parse_args(["clean"])
            cli._prompt_menu(args)
        return args, asked

    def test_empty_trash_question_demands_the_full_word(self):
        args, asked = self._run_menu()
        trash = [(q, danger) for q, danger in asked if "Trash" in q]
        self.assertTrue(trash, "the menu never asked about the Trash")
        for question, danger in trash:
            self.assertTrue(danger, f"not a danger prompt: {question}")

    def test_yes_here_is_what_skips_the_later_gate(self):
        args, _ = self._run_menu(answers=True)
        self.assertTrue(args.empty_trash)
        self.assertTrue(getattr(args, "_empty_trash_confirmed", False))

    def test_no_here_leaves_the_trash_alone(self):
        args, _ = self._run_menu(answers=False)
        self.assertFalse(args.empty_trash)
        self.assertFalse(getattr(args, "_empty_trash_confirmed", False))


class TestEmptyTrashWhenNothingMatched(unittest.TestCase):
    """The Trash prompt is answered before the scan runs. When the filters then
    match nothing, cmd_clean returns early and never reaches the empty step -
    so it has to say so, or a full-word 'yes' just vanishes."""

    class _Session:
        expunge_notice = None
        supports_gmail_search = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def select(self, folder, readonly=True):
            return 0

        def search_standard(self, criteria):
            return []

        def fetch_summaries(self, uids, on_progress=None, full_headers=False):
            return []

        def empty_trash(self, *a, **k):
            raise AssertionError("must not empty the Trash on an empty match set")

    def _run(self, **flags):
        args = cli.build_parser().parse_args(["clean", "--yes"])
        for k, v in flags.items():
            setattr(args, k, v)
        out = []
        acct = config.Account("me@x.com", "pw", "h", 993, "custom", None)
        with mock.patch.object(cli.config, "resolve_account", lambda a: acct),                 mock.patch.object(cli, "ImapSession", lambda *a, **k: self._Session()),                 mock.patch.object(cli.ui, "warn", lambda m: out.append(("warn", m))),                 mock.patch.object(cli.ui, "ok", lambda m: out.append(("ok", m))),                 mock.patch.object(cli.ui, "info", lambda m: out.append(("info", m))),                 mock.patch("builtins.print", lambda *a, **k: None):
            rc = cli.cmd_clean(args)
        return rc, out

    def test_it_says_the_trash_was_left_alone(self):
        rc, out = self._run(empty_trash=True)
        self.assertEqual(rc, 0)
        warned = " ".join(m for kind, m in out if kind == "warn")
        self.assertIn("Trash was left as it is", warned)

    def test_no_such_note_when_the_trash_was_never_asked_about(self):
        rc, out = self._run(empty_trash=False)
        self.assertEqual(rc, 0)
        self.assertNotIn("Trash", " ".join(m for _, m in out))


class TestEmptyResultIsExplained(unittest.TestCase):
    """--older-than is a minimum age. Read as a range - "5y" meaning "the last
    five years" - it asks for mail older than anything in the folder and gets a
    truthful nothing, which looks like the tool is broken."""

    class _Session:
        supports_gmail_search = False

        def __init__(self, oldest_date="2025-11-12", all_uids=("704344", "716330"),
                     held=None):
            self.all_uids = list(all_uids)
            self.oldest_date = oldest_date
            self.held = held
            self.fetched = []

        def folder_message_count(self, folder):
            return self.held

        def search_standard(self, criteria):
            return self.all_uids if criteria == ["ALL"] else []

        def fetch_summaries(self, uids, on_progress=None, full_headers=False):
            self.fetched.append(list(uids))
            return [_with_date(_mail(uids[0]), self.oldest_date)] if uids else []

    def _capture(self, session, filters, folder="INBOX"):
        out = []
        with mock.patch.object(cli.ui, "info", lambda m: out.append(m)):
            cli._explain_no_matches(session, filters, folder)
        return " ".join(out)

    def test_it_spells_out_what_the_age_actually_means(self):
        text = self._capture(self._Session(), Filters(older_than_days=1825))
        self.assertIn("arrived before", text)
        self.assertIn("not mail from the last", text)

    def test_it_names_the_oldest_message_in_the_folder(self):
        session = self._Session(oldest_date="2025-11-12")
        text = self._capture(session, Filters(older_than_days=1825))
        self.assertIn("2025-11-12", text)
        # the lowest uid is the oldest, whatever order the server listed them
        self.assertEqual(session.fetched, [["704344"]])

    def test_it_warns_when_the_server_hides_most_of_the_folder(self):
        # Yahoo: STATUS says 235013, the selected view exposes 10000. Reporting
        # the oldest of those as "the oldest message in INBOX" is a lie.
        session = self._Session(all_uids=("704344", "716330"), held=235013)
        out = []
        with mock.patch.object(cli.ui, "info", lambda m: out.append(m)),                 mock.patch.object(cli.ui, "warn", lambda m: out.append(m)):
            cli._explain_no_matches(session, Filters(older_than_days=1825), "INBOX")
        text = " ".join(out)
        self.assertIn("235,013", text)
        self.assertIn("newest 2", text)
        self.assertIn("this server will show", text)

    def test_it_claims_the_real_oldest_when_nothing_is_hidden(self):
        session = self._Session(all_uids=("704344", "716330"), held=2)
        text = self._capture(session, Filters(older_than_days=1825))
        self.assertIn("The oldest message in", text)
        self.assertNotIn("will show", text)

    def test_an_empty_folder_says_so(self):
        session = self._Session(all_uids=())
        self.assertIn("no messages at all", self._capture(session, Filters()))

    def test_a_failing_probe_is_not_fatal(self):
        session = self._Session()
        session.search_standard = lambda c: (_ for _ in ()).throw(CleanerError("nope"))
        self._capture(session, Filters())  # must not raise

    def test_no_note_when_the_age_filter_is_off(self):
        # scan() only calls this with an age set; guard the contract anyway
        args = cli.build_parser().parse_args(["scan", "--older-than", "0"])
        self.assertEqual(cli._make_filters(args).older_than_days, 0)


class _SlidingServer:
    """A folder that only shows part of itself, the way Yahoo shows the newest
    10000. Each batch that leaves lets the next one into view."""

    supports_gmail_search = False
    expunge_notice = None

    def __init__(self, batches):
        self.batches = [list(b) for b in batches]
        self.moved = []
        self.selects = 0
        self.emptied = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def select(self, folder, readonly=True):
        self.selects += 1
        return len(self.batches[0]) if self.batches else 0

    def folder_message_count(self, folder):
        return None

    def search_standard(self, criteria):
        return list(self.batches[0]) if self.batches else []

    def fetch_summaries(self, uids, on_progress=None, full_headers=False):
        return [_mail(u, unsub=["https://x.com/u"]) for u in uids]

    def find_trash_folder(self, hint=None):
        return "Trash"

    def move_to_trash(self, uids, trash, on_progress=None):
        self.moved.append(list(uids))
        if self.batches:
            self.batches.pop(0)  # that batch left, so the window slides
        return len(uids)

    def delete_permanently(self, uids, on_progress=None):
        return self.move_to_trash(uids, "Trash")

    def empty_trash(self, trash, on_progress=None):
        self.emptied += 1
        return 42


def _acct():
    return config.Account("me@x.com", "pw", "h", 993, "custom", None)


class TestRepeatPasses(unittest.TestCase):
    """--repeat exists because some servers only expose part of a folder. One
    pass can only ever see the current window; clearing it reveals the next."""

    def _drive(self, server, args, confirm=None):
        out = []
        patches = [
            mock.patch.object(cli.config, "resolve_account", lambda a: _acct()),
            mock.patch.object(cli, "ImapSession", lambda *a, **k: server),
            mock.patch.object(cli.ui, "ok", out.append),
            mock.patch.object(cli.ui, "info", out.append),
            mock.patch.object(cli.ui, "warn", out.append),
            mock.patch.object(cli.ui, "heading", out.append),
            mock.patch("builtins.print", lambda *a, **k: None),
        ]
        if confirm is not None:
            patches.append(mock.patch.object(cli.ui, "confirm", confirm))
        for p in patches:
            p.start()
        try:
            rc = cli.cmd_clean(args)
        finally:
            for p in reversed(patches):
                p.stop()
        return rc, " ".join(out)

    def _run(self, batches, argv=("clean", "--yes", "--repeat"), **flags):
        server = _SlidingServer(batches)
        args = cli.build_parser().parse_args(list(argv))
        for k, v in flags.items():
            setattr(args, k, v)
        rc, text = self._drive(server, args)
        return rc, server, text

    def test_it_keeps_going_until_a_pass_matches_nothing(self):
        rc, server, text = self._run([["1", "2"], ["3"], ["4", "5", "6"]])
        self.assertEqual(rc, 0)
        self.assertEqual(server.moved, [["1", "2"], ["3"], ["4", "5", "6"]])
        self.assertIn("6 emails moved to Trash over 3 pass(es)", text)
        self.assertIn("nothing left to match", text)

    def test_it_reopens_the_folder_each_pass(self):
        # re-selecting is what exposes the mail the last pass made room for
        _, server, _ = self._run([["1"], ["2"], ["3"]])
        self.assertEqual(server.selects, 4)  # 3 productive passes + the empty one

    def test_one_pass_without_the_flag(self):
        _, server, text = self._run([["1"], ["2"]], argv=("clean", "--yes"))
        self.assertEqual(server.moved, [["1"]])
        self.assertNotIn("pass(es)", text)

    def test_max_passes_caps_it(self):
        batches = [[str(i)] for i in range(10)]
        _, server, text = self._run(batches, max_passes=3)
        self.assertEqual(len(server.moved), 3)
        self.assertIn("reached the 3-pass limit", text)

    def test_it_asks_once_not_once_per_pass(self):
        server = _SlidingServer([["1"], ["2"], ["3"]])
        args = cli.build_parser().parse_args(["clean", "--repeat"])
        asked = []

        def confirm(question, danger=False):
            asked.append(question)
            return True

        self._drive(server, args, confirm=confirm)
        self.assertEqual(len(asked), 1, "asked %d times: %s" % (len(asked), asked))
        self.assertIn("repeat until nothing matches", asked[0])
        self.assertEqual(len(server.moved), 3)

    def test_declining_moves_nothing(self):
        server = _SlidingServer([["1"], ["2"]])
        args = cli.build_parser().parse_args(["clean", "--repeat"])
        rc, _ = self._drive(server, args, confirm=lambda q, danger=False: False)
        self.assertEqual(rc, 1)
        self.assertEqual(server.moved, [])

    def test_the_trash_is_emptied_once_at_the_end(self):
        _, server, text = self._run([["1"], ["2"], ["3"]], empty_trash=True)
        self.assertEqual(server.emptied, 1)
        self.assertEqual(len(server.moved), 3)
        self.assertIn("Trash emptied", text)

    def test_a_pass_that_moves_nothing_stops_the_loop(self):
        server = _SlidingServer([["1"], ["2"]])
        server.move_to_trash = lambda uids, trash, on_progress=None: 0
        args = cli.build_parser().parse_args(["clean", "--yes", "--repeat"])
        _, text = self._drive(server, args)
        self.assertIn("a pass moved nothing", text)

    def test_stopping_it_still_reports_what_was_done(self):
        server = _SlidingServer([["1", "2"], ["3"], ["4"]])
        real_move = server.move_to_trash
        calls = []

        def move(uids, trash, on_progress=None):
            calls.append(uids)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_move(uids, trash)

        server.move_to_trash = move
        args = cli.build_parser().parse_args(["clean", "--yes", "--repeat"])
        rc, text = self._drive(server, args)
        self.assertEqual(rc, 0)
        self.assertIn("you stopped it", text)
        self.assertIn("2 emails moved to Trash", text)  # the first pass survived


class TestResolveAiSettings(unittest.TestCase):
    KEYS = ("EMAIL_CLEANER_AI_BACKEND", "EMAIL_CLEANER_AI_MODEL",
            "EMAIL_CLEANER_AI_API_KEY", "EMAIL_CLEANER_AI_HOST",
            "EMAIL_CLEANER_AI_PROMPT")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _args(self, **over):
        blank = dict(ai=True, ai_backend="ollama", ai_model=None,
                     ai_prompt="junk", ai_snippet=False)
        blank.update(over)
        return type("Args", (), blank)()

    def test_off_when_ai_flag_absent(self):
        self.assertIsNone(config.resolve_ai_settings(self._args(ai=False)))

    def test_no_backend_raises(self):
        with self.assertRaises(CleanerError):
            config.resolve_ai_settings(self._args(ai_backend=None))

    def test_unknown_backend_raises(self):
        with self.assertRaises(CleanerError):
            config.resolve_ai_settings(self._args(ai_backend="frobnicate"))

    def test_no_prompt_raises(self):
        with self.assertRaises(CleanerError):
            config.resolve_ai_settings(self._args(ai_prompt=None))

    def test_hosted_backend_needs_key(self):
        with self.assertRaises(CleanerError):
            config.resolve_ai_settings(self._args(ai_backend="openai"))

    def test_ollama_defaults_resolve(self):
        s = config.resolve_ai_settings(self._args())
        self.assertEqual(s.backend, "ollama")
        self.assertEqual(s.model, "llama3.1")
        self.assertEqual(s.host, "http://localhost:11434")
        self.assertTrue(s.is_local)

    def test_flag_overrides_model(self):
        s = config.resolve_ai_settings(self._args(ai_model="mistral"))
        self.assertEqual(s.model, "mistral")

    def test_env_supplies_prompt_when_flag_absent(self):
        os.environ["EMAIL_CLEANER_AI_PROMPT"] = "kill the newsletters"
        s = config.resolve_ai_settings(self._args(ai_prompt=None))
        self.assertEqual(s.prompt, "kill the newsletters")

    def test_host_without_a_scheme_is_rejected_up_front(self):
        os.environ["EMAIL_CLEANER_AI_API_KEY"] = "sk-xyz"
        os.environ["EMAIL_CLEANER_AI_HOST"] = "api.openai.com/v1"
        with self.assertRaises(CleanerError):
            config.resolve_ai_settings(self._args(ai_backend="openai"))

    def test_custom_host_with_a_scheme_resolves(self):
        os.environ["EMAIL_CLEANER_AI_HOST"] = "http://192.168.1.5:11434"
        s = config.resolve_ai_settings(self._args())
        self.assertEqual(s.host, "http://192.168.1.5:11434")

    def test_hosted_backend_with_key_resolves(self):
        os.environ["EMAIL_CLEANER_AI_API_KEY"] = "sk-xyz"
        s = config.resolve_ai_settings(self._args(ai_backend="anthropic"))
        self.assertEqual(s.backend, "anthropic")
        self.assertEqual(s.model, "claude-haiku-4-5")
        self.assertEqual(s.api_key, "sk-xyz")
        self.assertTrue(s.is_hosted)


if __name__ == "__main__":
    unittest.main()
