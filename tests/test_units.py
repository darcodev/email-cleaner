"""Tests for the pure functions, no network needed.

Run with:  python -m unittest discover -s tests -v
"""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from email_cleaner import config
from email_cleaner.ai import (
    AISettings,
    Classifier,
    Verdict,
    _BackendError,
    _loads_lenient,
    _parse_verdicts,
)
from email_cleaner.errors import CleanerError
from email_cleaner.imap_client import (
    EmailSummary,
    _clean_snippet,
    _parse_fetch_response,
    _parse_snippet_response,
    decode_mime_header,
    extract_unsubscribe_urls,
    quote_imap_string,
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
    summarize_senders,
)
from email_cleaner.ui import human_size, truncate


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


class TestLenientJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(_loads_lenient('{"a": 1}'), {"a": 1})

    def test_wrapped_in_prose_and_fences(self):
        raw = 'Sure!\n```json\n{"results": []}\n```\nHope that helps'
        self.assertEqual(_loads_lenient(raw), {"results": []})

    def test_bare_array(self):
        self.assertEqual(_loads_lenient("[1, 2, 3]"), [1, 2, 3])

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

    def test_hosted_backend_with_key_resolves(self):
        os.environ["EMAIL_CLEANER_AI_API_KEY"] = "sk-xyz"
        s = config.resolve_ai_settings(self._args(ai_backend="anthropic"))
        self.assertEqual(s.backend, "anthropic")
        self.assertEqual(s.model, "claude-haiku-4-5")
        self.assertEqual(s.api_key, "sk-xyz")
        self.assertTrue(s.is_hosted)


if __name__ == "__main__":
    unittest.main()
