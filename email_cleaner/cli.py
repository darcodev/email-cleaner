"""Command line interface.

Commands:
  scan          show what would be cleaned (default, read-only)
  clean         move matching email to Trash (or delete with --permanent)
  unsubscribe   export unsubscribe links for your noisiest senders
"""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__, config, ui
from .ai import Classifier
from .errors import CleanerError, ConnectionLost
from .imap_client import ImapSession
from .scanner import (
    Filters,
    VALID_CATEGORIES,
    collect_unsubscribe_links,
    imap_date,
    parse_age,
    scan,
    summarize_senders,
)

EXAMPLES = """\
examples:
  email-cleaner                            interactive menu: pick your criteria, then clean
  email-cleaner scan                       one-shot read-only preview, no menu (deletes nothing)
  email-cleaner clean                      move promos older than 30 days to Trash
  email-cleaner clean --older-than 2y      only promos older than 2 years
  email-cleaner clean --keyword sale --keyword "50% off"
  email-cleaner clean --all --older-than 1y --keyword newsletter
  email-cleaner clean --from doordash.com --older-than 0
  email-cleaner scan --category social --category updates
  email-cleaner clean --protect amazon.com --protect boss@work.com
  email-cleaner clean --empty-trash        move to Trash, then empty Trash (permanent)
  email-cleaner unsubscribe                unsubscribe links for the noisiest senders
  email-cleaner scan --ai --ai-prompt "marketing, but keep orders and receipts"

setup:
  set EMAIL_CLEANER_EMAIL and EMAIL_CLEANER_PASSWORD in your environment.
  The password must be an app password, not your normal login password.
  See the README for where to create one.

safety:
  'scan' is read-only. 'clean' moves messages to Trash, where your provider
  keeps them ~30 days. Starred mail and --protect senders are always skipped.
  --empty-trash and --permanent skip that safety net and delete for good.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="email-cleaner",
        description="Clean promotional email out of your inbox from the command line.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    account = common.add_argument_group("account (overrides the environment variables)")
    account.add_argument("--email", help="email address to clean")
    account.add_argument("--provider", help="gmail, outlook, yahoo, icloud, or custom")
    account.add_argument("--host", help="IMAP host for custom providers")
    account.add_argument("--port", type=int, help="IMAP port (default 993)")
    account.add_argument("--trash-folder", help="name of the Trash folder, if auto-detect fails")

    filters = common.add_argument_group("filters (you decide what gets cleaned)")
    filters.add_argument(
        "--older-than",
        default="30d",
        metavar="AGE",
        help="only mail older than this: 30d, 3m, 2y, or 0 for any age (default: 30d)",
    )
    filters.add_argument(
        "--keyword",
        action="append",
        default=[],
        metavar="WORD",
        help="only mail containing this word or phrase; repeatable (matches any)",
    )
    filters.add_argument(
        "--from",
        dest="from_senders",
        action="append",
        default=[],
        metavar="SENDER",
        help="only mail from this address or domain; repeatable (matches any)",
    )
    filters.add_argument(
        "--all",
        action="store_true",
        help="don't limit to promotional mail; combine with --older-than/--keyword/--from",
    )
    filters.add_argument(
        "--category",
        action="append",
        choices=VALID_CATEGORIES,
        help="Gmail category to clean; repeatable (default: promotions)",
    )
    filters.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="SENDER",
        help="never touch mail from this address or domain; repeatable",
    )
    filters.add_argument(
        "--include-starred",
        action="store_true",
        help="also match starred/important mail (skipped by default)",
    )
    filters.add_argument("--limit", type=int, help="stop after this many matches")
    filters.add_argument("--folder", default="INBOX", help="folder to search (default: INBOX)")

    ai = common.add_argument_group(
        "AI classification (optional, opt-in; needs a backend and a rule)"
    )
    ai.add_argument(
        "--ai",
        action="store_true",
        help="run an AI keep/delete pass over the matches (only ever narrows them)",
    )
    ai.add_argument(
        "--ai-prompt",
        metavar="RULE",
        help='plain-language rule for what to delete, e.g. "newsletters but keep receipts"',
    )
    ai.add_argument(
        "--ai-backend",
        help="ollama, openai or anthropic (overrides EMAIL_CLEANER_AI_BACKEND)",
    )
    ai.add_argument("--ai-model", help="model id (overrides EMAIL_CLEANER_AI_MODEL)")
    ai.add_argument(
        "--ai-explain",
        action="store_true",
        help="show the model's one-line reason for each match it keeps for deletion",
    )
    ai.add_argument(
        "--ai-snippet",
        action="store_true",
        help="also send a short body snippet to the model (fetches a slice of the body)",
    )
    common.add_argument("--no-color", action="store_true", help="disable colored output")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser(
        "scan",
        parents=[common],
        help="preview what would be cleaned (read-only, default command)",
    )
    clean = sub.add_parser("clean", parents=[common], help="move matching email to Trash")
    clean.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    clean.add_argument(
        "--permanent",
        action="store_true",
        help="permanently delete instead of moving to Trash (NOT recoverable)",
    )
    clean.add_argument(
        "--empty-trash",
        action="store_true",
        help="after moving, empty the whole Trash folder to delete for good (NOT recoverable)",
    )
    clean.add_argument(
        "--repeat",
        action="store_true",
        help=(
            "keep going in passes until a pass matches nothing. For servers that "
            "only show part of a folder (Yahoo caps IMAP at the newest 10000 and "
            "one search at 1000), each pass lets older mail into view"
        ),
    )
    clean.add_argument(
        "--max-passes",
        type=int,
        default=25,
        metavar="N",
        help="stop --repeat after this many passes (default 25)",
    )
    unsub = sub.add_parser(
        "unsubscribe",
        parents=[common],
        help="export unsubscribe links for the senders that mail you most",
    )
    unsub.add_argument("--output", metavar="FILE", help="also write the links to a file")
    return parser


def _make_filters(args) -> Filters:
    return Filters(
        older_than_days=parse_age(args.older_than),
        categories=args.category or ["promotions"],
        keywords=args.keyword,
        from_senders=args.from_senders,
        protected_senders=args.protect,
        promo_only=not args.all,
        include_starred=args.include_starred,
        limit=args.limit,
    )


def _build_classifier(args) -> Classifier | None:
    """Resolve the AI settings and build a classifier, or None when --ai is off."""
    settings = config.resolve_ai_settings(args)
    return Classifier(settings) if settings is not None else None


def _confirm_hosted_ai(classifier, args, allow_confirm: bool) -> bool:
    """Warn before any mail leaves the machine for a hosted backend, and (for
    clean) confirm it. Returns False only if the user declines. Local backends
    like Ollama send nothing off-box, so they need neither warning nor prompt."""
    if classifier is None or not classifier.settings.is_hosted:
        return True
    extra = " and a short body snippet" if classifier.settings.snippet else ""
    ui.warn(
        f"AI backend is {classifier.settings.provider_host}. The sender and "
        f"subject{extra} of each matching message will be sent there for "
        "classification."
    )
    if allow_confirm and not getattr(args, "yes", False):
        if not ui.confirm("Send those details to classify them?"):
            return False
    return True


def _explain_no_matches(session: ImapSession, filters: Filters, folder: str) -> None:
    """Say why an age-filtered run came back empty.

    Almost always the same cause: --older-than is a minimum age and gets read
    as a range, so the run asks for mail older than anything in the folder and
    gets a truthful nothing. Two extra round trips beat a dead end, and they
    only happen when there was nothing to report anyway.
    """
    cutoff = imap_date(filters.older_than_days)
    ui.info(
        f"Note: 'older than {filters.older_than_days} days' means mail that "
        f"arrived before {cutoff} - not mail from the last "
        f"{filters.older_than_days} days."
    )
    try:
        uids = session.search_standard(["ALL"])
        if not uids:
            ui.info(f"'{folder}' has no messages at all.")
            return
        # STATUS counts the whole folder; the search only sees what the server
        # puts in the selected view. Yahoo reports a 235k Inbox and then hands
        # IMAP the newest 10000 of it, so saying "the oldest message in INBOX"
        # off the search alone would be a confident lie.
        held = session.folder_message_count(folder)
        if held is not None and held > len(uids):
            ui.warn(
                f"'{folder}' holds {held:,} messages, but this server only "
                f"exposes the newest {len(uids):,} of them over IMAP. Anything "
                "older cannot be reached by any IMAP client, this one included."
            )
        # lowest UID is the oldest message, whatever order the server listed them
        oldest = session.fetch_summaries([min(uids, key=int)])
    except CleanerError:
        return  # best effort - the run itself already finished
    if oldest and oldest[0].date:
        reach = "the oldest message" if held is None or held <= len(uids) else (
            "the oldest message this server will show"
        )
        ui.info(f"{reach.capitalize()} in '{folder}' is from {oldest[0].date}.")


def _run_scan(session: ImapSession, args, filters: Filters, classifier=None):
    ui.info(f"Searching '{args.folder}' ...")
    result = scan(
        session,
        filters,
        on_progress=lambda d, t: ui.progress(d, t, "Reading headers"),
        classifier=classifier,
        on_ai_progress=lambda d, t: ui.progress(d, t, "Classifying"),
        on_notice=ui.info,
    )
    if result.skipped_protected or result.skipped_starred:
        ui.info(
            f"Skipped {result.skipped_protected} protected and "
            f"{result.skipped_starred} starred message(s)."
        )
    if result.skipped_not_promo:
        # only ever set on the fallback path, where the count would otherwise
        # look mysteriously small next to what the server was asked for
        ui.info(
            f"This server can't search mail headers, so {result.skipped_not_promo} "
            "non-promotional message(s) were filtered out here instead."
        )
    if classifier is not None:
        if result.skipped_ai:
            ui.info(f"AI kept {result.skipped_ai} message(s) it judged not a match.")
        if classifier.transport_error:
            ui.warn(f"{classifier.transport_error}; those messages were kept.")
    if not result.emails and filters.older_than_days > 0:
        _explain_no_matches(session, filters, args.folder)
    return result


def _show_report(result, preview_rows: int = 15, explain: bool = False) -> None:
    emails = result.emails
    if not emails:
        ui.ok("Nothing matched. Your inbox is already clean.")
        return

    ui.heading(f"Top senders ({len(emails)} matching emails)")
    rows = [
        [str(count), ui.human_size(size), sender]
        for sender, count, size in summarize_senders(emails)
    ]
    print(ui.table(["#", "Size", "Sender"], rows))

    ui.heading(f"Oldest matches (showing {min(preview_rows, len(emails))} of {len(emails)})")
    if explain and result.ai_reasons:
        headers = ["Date", "From", "Subject", "Why (AI)"]
        rows = [
            [e.date, e.sender_display, e.subject, result.ai_reasons.get(e.uid, "")]
            for e in emails[:preview_rows]
        ]
    else:
        headers = ["Date", "From", "Subject"]
        rows = [[e.date, e.sender_display, e.subject] for e in emails[:preview_rows]]
    print(ui.table(headers, rows))

    print()
    ui.info(
        f"Total: {len(emails)} emails, ~{ui.human_size(result.total_size)} "
        f"({result.query_description})"
    )


def cmd_scan(args) -> int:
    account = config.resolve_account(args)
    filters = _make_filters(args)
    classifier = _build_classifier(args)
    # scan is read-only, but a hosted backend still sends data off-box, so warn
    _confirm_hosted_ai(classifier, args, allow_confirm=False)
    with ImapSession(account.host, account.port, account.address, account.password) as session:
        ui.ok(f"Connected to {account.host} as {account.address}")
        session.select(args.folder, readonly=True)
        result = _run_scan(session, args, filters, classifier)
    _show_report(result, explain=args.ai_explain)
    if result.emails:
        print()
        ui.info("This was a preview, nothing was changed.")
        ui.info("Run 'email-cleaner clean' to move these to Trash.")
    return 0


# How many times a --repeat run will re-establish a dropped session before
# giving up. Yahoo hangs up every few hundred moved messages, so a long run
# needs several; a server that drops instantly should not be retried forever.
_MAX_RECONNECTS = 5


def _confirm_clean(args, count: int, filters: Filters, repeating: bool) -> bool:
    """The gate before anything moves. True to go ahead.

    Asked once per run, not once per pass: --repeat is approved as a whole, so
    the answer has to say what it is approving - an unbounded series of moves,
    not the one batch on screen.
    """
    # --all with no age and no keywords means the whole folder matched,
    # so be extra careful even for a trash move
    wide_open = args.all and filters.older_than_days == 0 and not (
        filters.keywords or filters.from_senders
    )
    more = " and keep going until nothing matches" if repeating else ""
    if args.permanent:
        ui.warn(f"--permanent will delete {count} emails FOREVER. No Trash, no undo.")
        if repeating:
            ui.warn(f"--repeat will do this again every pass, up to {args.max_passes}.")
        return args.yes or ui.confirm(f"Permanently delete them{more}?", danger=True)
    if wide_open:
        # matches the whole folder; warn even under --yes, only the
        # prompt itself is skippable
        ui.warn(f"This matches every message in '{args.folder}'.")
        return args.yes or ui.confirm(
            f"Move all {count} of them to Trash{more}?", danger=True
        )
    if repeating:
        return args.yes or ui.confirm(
            f"Move {count} emails to Trash and repeat until nothing matches "
            f"(up to {args.max_passes} passes, recoverable in Trash)?",
            danger=True,
        )
    return args.yes or ui.confirm(
        f"Move {count} emails to Trash (recoverable there for ~30 days)?"
    )


def _empty_trash_step(session, args, trash: str):
    """Empty the Trash if it was asked for. Returns the count, or None."""
    print()
    ui.warn("Emptying the Trash permanently deletes everything in it - no undo.")
    # a menu run already asked; a --yes run opted out of prompts
    pre_ok = args.yes or getattr(args, "_empty_trash_confirmed", False)
    if pre_ok or ui.confirm("Empty the Trash now?", danger=True):
        return session.empty_trash(
            trash, on_progress=lambda d, t: ui.progress(d, t, "Emptying Trash")
        )
    ui.info("Left the Trash as-is.")
    return None


def cmd_clean(args) -> int:
    account = config.resolve_account(args)
    filters = _make_filters(args)
    classifier = _build_classifier(args)
    # for a hosted backend, confirm before any mail leaves the machine - this
    # has to happen before the scan, which is where the classification runs
    if not _confirm_hosted_ai(classifier, args, allow_confirm=True):
        ui.info("Cancelled, nothing was changed.")
        return 1
    started = time.monotonic()
    repeating = getattr(args, "repeat", False)
    max_passes = max(1, args.max_passes) if repeating else 1

    moved_total = 0
    bytes_total = 0
    passes_run = 0
    reconnects = 0
    lost = None  # set when the session died for good, so the exit code says so
    action = "moved to Trash"
    emptied = None
    stopped_by = ""

    with ImapSession(account.host, account.port, account.address, account.password) as session:
        ui.ok(f"Connected to {account.host} as {account.address}")
        approved = False
        trash = None
        for attempt in range(1, max_passes + 1):
            # re-open the folder each pass: on a server that only exposes part
            # of it, this is what lets the mail we just moved out be replaced
            # by older mail we could not previously see
            session.select(args.folder, readonly=False)
            if repeating:
                ui.heading(f"Pass {attempt} of at most {max_passes}")
            try:
                result = _run_scan(session, args, filters, classifier)
                if attempt == 1:
                    _show_report(result, explain=args.ai_explain)
                elif result.emails:
                    ui.info(
                        f"{len(result.emails)} match(es), "
                        f"~{ui.human_size(result.total_size)}."
                    )
                if not result.emails:
                    stopped_by = "nothing left to match" if passes_run else "nothing matched"
                    break

                print()
                if not approved:
                    if not _confirm_clean(args, len(result.emails), filters, repeating):
                        ui.info("Cancelled, nothing was changed.")
                        return 1
                    approved = True  # a repeat run is approved once, not per pass

                uids = [e.uid for e in result.emails]
                if args.permanent:
                    done = session.delete_permanently(
                        uids, on_progress=lambda d, t: ui.progress(d, t, "Deleting")
                    )
                    action = "deleted"
                else:
                    trash = trash or session.find_trash_folder(account.trash_folder)
                    done = session.move_to_trash(
                        uids, trash,
                        on_progress=lambda d, t: ui.progress(d, t, "Moving to Trash"),
                    )
            except ConnectionLost as exc:
                # nothing is half-done: the next pass re-runs its own search, so
                # whatever already moved stays moved and the rest is picked up
                # again. Only worth retrying when there are passes left to use.
                if not repeating or reconnects >= _MAX_RECONNECTS:
                    lost = exc
                    stopped_by = "the server hung up"
                    break
                reconnects += 1
                ui.warn(
                    f"{exc} Reconnecting ({reconnects} of {_MAX_RECONNECTS}) "
                    "and carrying on."
                )
                try:
                    session.reconnect()
                except CleanerError as again:
                    lost = again
                    stopped_by = "could not reconnect"
                    break
                continue
            except KeyboardInterrupt:
                # "until I say stop" - report what did happen rather than
                # unwinding to main() and losing the tally
                stopped_by = "you stopped it"
                print()
                break

            passes_run = attempt
            moved_total += done
            bytes_total += result.total_size
            if done == 0:
                # nothing actually left the folder, so the next pass would see
                # the same mail again; stop rather than spin
                stopped_by = "a pass moved nothing"
                break
        else:
            stopped_by = f"reached the {max_passes}-pass limit"

        if args.empty_trash and not args.permanent:
            if moved_total:
                trash = trash or session.find_trash_folder(account.trash_folder)
                emptied = _empty_trash_step(session, args, trash)
            else:
                # the full-word Trash prompt was answered before the scan ran.
                # Going quiet here reads as "it emptied"; say plainly that it
                # did not, because that answer was for a step we never reached.
                ui.warn(
                    "Nothing matched, so nothing moved - and the Trash was left "
                    "as it is. Emptying it only runs after a move."
                )
        expunge_notice = session.expunge_notice

    elapsed = time.monotonic() - started
    print()
    if moved_total:
        over = f" over {passes_run} pass(es)" if repeating else ""
        ui.ok(
            f"{moved_total} emails {action}{over}, reclaimed "
            f"~{ui.human_size(bytes_total)} in {elapsed:.1f}s."
        )
    if repeating and stopped_by:
        ui.info(f"Stopped: {stopped_by}.")
    if expunge_notice:
        ui.warn(expunge_notice)
    if emptied is not None:
        ui.ok(f"Trash emptied: {emptied} message(s) permanently deleted.")
    if lost is not None:
        # whatever moved above is real and already reported; this says why the
        # run ended early, and makes the exit code admit it rather than
        # returning success on a session that died halfway through
        ui.error(str(lost), hint=lost.hint)
        return 2
    return 0


def cmd_unsubscribe(args) -> int:
    account = config.resolve_account(args)
    filters = _make_filters(args)
    with ImapSession(account.host, account.port, account.address, account.password) as session:
        ui.ok(f"Connected to {account.host} as {account.address}")
        session.select(args.folder, readonly=True)
        result = _run_scan(session, args, filters)

    links = collect_unsubscribe_links(result.emails)
    if not links:
        ui.info("No unsubscribe links found in the matching emails.")
        return 0

    ui.heading(f"Unsubscribe links ({len(links)} senders)")
    rows = [[str(count), sender, url] for sender, count, url in links]
    print(ui.table(["#", "Sender", "Unsubscribe"], rows))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            for sender, count, url in links:
                fh.write(f"{sender}\t{count}\t{url}\n")
        print()
        ui.ok(f"Wrote {len(links)} links to {args.output}")
    return 0


def _split_list(text: str) -> list[str]:
    # comma OR space separated, whichever the user felt like typing
    parts = text.replace(",", " ").split()
    return [p.strip() for p in parts if p.strip()]


def _prompt_menu(args) -> None:
    """Ask what to clean *before* we go read the whole mailbox, and fill in
    args. Only runs on a bare, interactive invocation."""
    ui.heading("What do you want to go through?")
    print("  1. Promotions - marketing, deals, newsletters (default)")
    print("  2. A Gmail category - social, updates or forums")
    print("  3. Everything - any mail that matches the filters below")
    choice = ui.prompt("Pick 1-3", default="1")

    if choice == "2":
        raw = ui.prompt("Which? social / updates / forums (comma-separated)", default="updates")
        picks = [c.lower() for c in _split_list(raw) if c.lower() in VALID_CATEGORIES]
        args.category = picks or ["updates"]
        args.all = False
    elif choice == "3":
        args.all = True
    else:
        args.all = False  # promotions is the default (category stays unset)

    ui.heading("How old does it have to be?")
    # "How far back?" read as a range - people answered 5y meaning "search the
    # last five years" and got nothing, because it is a floor: 5y asks for mail
    # from before 2021 and most inboxes do not go back that far
    print("  a minimum age, not a range - 6m means mail from MORE than six")
    print("  months ago, and nothing newer. e.g. 30d, 6m, 2y. Use 0 for any age.")
    args.older_than = ui.prompt("Only mail older than", default="30d")

    ui.heading("Narrow it down (optional, just hit Enter to skip)")
    args.keyword = _split_list(ui.prompt("Only mail containing these words", default=""))
    args.from_senders = _split_list(ui.prompt("Only from these senders/domains", default=""))
    args.protect = _split_list(ui.prompt("Never touch these senders", default=""))

    ui.heading("One pass, or keep going?")
    print("  some servers only show part of a folder at a time - Yahoo shows the")
    print("  newest 10000 - so one pass can only reach so far back. Repeating lets")
    print("  older mail into view as the mail you clean leaves the folder.")
    args.repeat = ui.confirm("Keep going until nothing matches?")

    # answering here is what lets cmd_clean skip its own gate later, so this has
    # to be the same 'type yes in full' one the --empty-trash flag gets - a
    # stray 'y' at a menu must not be enough to wipe the whole Trash folder
    args.empty_trash = ui.confirm(
        "After trashing, empty the whole Trash folder too? (permanent, optional)",
        danger=True,
    )
    if args.empty_trash:
        # they answered here, so we won't ask a second time when we get to it
        args._empty_trash_confirmed = True
    print()


def _interactive() -> bool:
    # a real terminal on both ends. if either is piped/redirected - or missing
    # entirely, like under pythonw where the streams can be None - we skip the
    # menu so cron jobs and 'email-cleaner | tee log' dont hang on input
    stdin_tty = getattr(sys.stdin, "isatty", lambda: False)()
    stdout_tty = getattr(sys.stdout, "isatty", lambda: False)()
    return stdin_tty and stdout_tty


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    try:
        # bare run in a terminal -> friendly menu that asks the criteria first,
        # then previews the matches and offers to clean them
        if not argv and _interactive():
            args = parser.parse_args(["clean"])  # clean namespace with all defaults
            if getattr(args, "no_color", False):
                ui.disable_colors()
            _prompt_menu(args)
            return cmd_clean(args)

        # scan is the default command otherwise, so 'email-cleaner --older-than 2y'
        # behaves like 'email-cleaner scan --older-than 2y'. Slip 'scan' in unless
        # the first arg is already a command or a top-level flag, else argparse
        # rejects the scan flags before we get a chance to look at them.
        if not argv:
            argv = ["scan"]  # piped/redirected: safe read-only default
        elif argv[0] not in ("scan", "clean", "unsubscribe", "-h", "--help", "--version"):
            argv = ["scan", *argv]

        args = parser.parse_args(argv)
        if getattr(args, "no_color", False):
            ui.disable_colors()

        handlers = {
            "scan": cmd_scan,
            "clean": cmd_clean,
            "unsubscribe": cmd_unsubscribe,
        }
        return handlers[args.command](args)
    except CleanerError as exc:
        ui.error(str(exc), hint=exc.hint)
        return 2
    except (KeyboardInterrupt, EOFError):
        print()
        ui.warn("Interrupted, nothing else was changed.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
