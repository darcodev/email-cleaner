"""Tests for the pure functions, no network needed.

Run with:  python -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from email_cleaner import config
from email_cleaner.errors import CleanerError
from email_cleaner.imap_client import (
    EmailSummary,
    _parse_fetch_response,
    decode_mime_header,
    extract_unsubscribe_urls,
    quote_imap_string,
)
from email_cleaner.providers import get_provider, guess_provider
from email_cleaner.scanner import (
    Filters,
    _apply_limit,
    build_gmail_query,
    build_standard_criteria,
    imap_date,
    is_protected,
    parse_age,
    summarize_senders,
)
from email_cleaner.ui import human_size, truncate


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


if __name__ == "__main__":
    unittest.main()
