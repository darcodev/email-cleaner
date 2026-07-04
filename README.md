# email-cleaner

Command line tool that clears promotional email out of your inbox over
IMAP. You decide what gets cleaned (age, keywords, senders, categories),
preview it first, and nothing is permanently deleted unless you ask for
that specifically. No dependencies, just the Python standard library.

```text
$ email-cleaner clean
Connected to imap.gmail.com as you@gmail.com
Searching 'INBOX' ...
  Reading headers 1,240/1,240 (100%)

Top senders (1,240 matching emails)
#    Size      Sender
-----------------------------------------------
312  48.1 MB   DoorDash <no-reply@doordash.com>
188  12.9 MB   Uber Eats <uber@uber.com>
143   6.2 MB   Old Navy <oldnavy@email.oldnavy.com>

Move 1,240 emails to Trash (recoverable there for ~30 days)? [y/N] y
  Moving to Trash 1,240/1,240 (100%)
1,240 emails moved to Trash, reclaimed ~81.4 MB in 9.2s.
```

## Install

Needs Python 3.9 or newer.

```bash
git clone <this repo>
cd email-cleaner
pip install .
```

Or skip the install and run `python -m email_cleaner` from the repo folder.

## Setup

Easiest way: copy `.env.example` to `.env` and fill in your details.

```bash
cp .env.example .env      # on Windows: copy .env.example .env
```

Then open `.env` and set your address and app password:

```ini
EMAIL_CLEANER_EMAIL=you@gmail.com
EMAIL_CLEANER_PASSWORD=your-16-char-app-password
```

That's it - run the tool from the project folder and it reads `.env`
automatically. To change your password later, just edit the file. The
`.env` file is gitignored, so it never gets committed.

If you'd rather not use a file, the same names work as real environment
variables (and those take priority over `.env`). If the password is left
blank, you'll simply be asked for it each run.

The IMAP server is detected from your address for Gmail, Outlook, Yahoo
and iCloud. Anything else: set `EMAIL_CLEANER_HOST` (and optionally
`EMAIL_CLEANER_PORT`, default 993) in `.env`, or pass `--host`.

### App passwords

Your normal password won't work over IMAP. You need an app password,
which your provider generates for you:

| Provider | Where |
| --- | --- |
| Gmail | https://myaccount.google.com/apppasswords (needs 2-Step Verification on) |
| Yahoo | Account Security > Generate app password |
| iCloud | https://appleid.apple.com/account/manage > App-Specific Passwords |
| Outlook | https://account.live.com/proofs/apppassword (may not work on all accounts) |

## Usage

Just run it with no arguments and it walks you through a short menu - what
kind of mail (promotions / a category / everything), how far back, and any
keywords or senders to narrow by - then previews the matches and asks before
touching anything:

```bash
email-cleaner            # or: python -m email_cleaner
```

Prefer to skip the menu? `scan` previews, `clean` does it, and both take the
same filters directly:

```bash
email-cleaner scan                          # read-only preview, no menu
email-cleaner clean                         # move promos older than 30 days to Trash
email-cleaner clean --yes                   # skip the confirmation
```

(The menu only shows in a real terminal; piped or scheduled runs fall back to
a read-only scan so nothing hangs waiting for input.)

You control what matches:

```bash
email-cleaner clean --older-than 2y         # only mail older than 2 years (d/m/y)
email-cleaner clean --older-than 0          # any age
email-cleaner clean --keyword sale --keyword "50% off"   # must contain one of these
email-cleaner clean --from doordash.com     # only from this sender/domain
email-cleaner clean --all --older-than 1y   # ALL mail (not just promos) older than a year
email-cleaner scan --category social --category updates  # other Gmail tabs
email-cleaner clean --protect amazon.com --protect boss@work.com
email-cleaner clean --limit 500             # cap how many get cleaned
email-cleaner clean --folder "[Gmail]/All Mail"          # search beyond the inbox
email-cleaner unsubscribe --output links.txt
email-cleaner clean --empty-trash           # move to Trash, then empty Trash for good
email-cleaner clean --permanent             # skip Trash, gone forever - be careful
```

`--keyword`, `--from`, `--category` and `--protect` can all be repeated.
Repeated keyword/from filters match if ANY of them hit; `--protect` always
wins over everything else.

Run `email-cleaner clean --help` for the full list.

## How it decides what's promotional

On Gmail it uses Gmail's own search over IMAP (`category:promotions`
plus your filters), the same classification as the Promotions tab. On
other IMAP servers it matches mail carrying a `List-Unsubscribe` header,
which bulk senders are required to include these days. `--all` turns
this off and matches everything your other filters allow.

Starred/important mail is always skipped unless you pass
`--include-starred`, and protected senders are checked client-side on
every message, even if the server search matched them.

## Safety

- `scan` never changes anything.
- `clean` moves mail to Trash and asks first. Your provider keeps Trash
  around ~30 days, so mistakes are recoverable.
- `clean --permanent` and `clean --empty-trash` are the destructive modes
  and make you type `yes` in full. `--empty-trash` empties the *entire*
  Trash folder after the move, so anything already sitting in Trash goes too.
  On Gmail this is also the real way to reclaim the space, since a plain
  delete just removes the label.
- Nothing you clean is logged or written to disk. The tool only ever reads
  your `.env` for credentials; it keeps no history of what it touched. The
  one exception is `unsubscribe --output`, which writes the file you ask for.

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT, see [LICENSE](LICENSE).
