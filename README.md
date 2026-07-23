# email-cleaner

[![CI](https://github.com/darcodev/email-cleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/darcodev/email-cleaner/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

Command line tool that clears promotional email out of your inbox over IMAP.
You decide what gets cleaned (age, keywords, senders, categories), you preview
it first, and nothing is permanently deleted unless you specifically ask for
that. It works with Gmail, Outlook, Yahoo and iCloud out of the box, and any
other IMAP server with one extra setting. No third-party packages, just the
Python standard library.

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

## Contents

- [Why this exists](#why-this-exists)
- [Install](#install)
- [Setup](#setup)
- [App passwords](#app-passwords)
- [Usage](#usage)
- [Filters in detail](#filters-in-detail)
- [The three ways to delete](#the-three-ways-to-delete)
- [How it decides what is promotional](#how-it-decides-what-is-promotional)
- [AI-assisted filtering (optional)](#ai-assisted-filtering-optional)
- [Recipes](#recipes)
- [Best practices](#best-practices)
- [Automating it](#automating-it)
- [Safety and privacy](#safety-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Exit codes](#exit-codes)
- [How it works](#how-it-works)
- [Contributing](#contributing)
- [License](#license)

## Why this exists

Inbox filters and the Gmail Promotions tab hide marketing mail, but they do not
delete the backlog. After a few years you can have tens of thousands of old
receipts, newsletters and deal emails eating your storage quota. This tool goes
through that backlog from the command line: you say what counts as junk, it
shows you exactly what matched, and only then does it move anything. It never
touches starred mail or senders you protect, and by default everything goes to
Trash, where your provider keeps it recoverable for about 30 days.

## Install

Needs Python 3.9 or newer. There are no third-party packages to install, only
the standard library.

The easiest way is [pipx](https://pipx.pypa.io/), which installs the
`email-cleaner` command into its own isolated environment and puts it on your
PATH. That avoids the "pip install broke my system Python" problem, and on
newer Debian, Ubuntu and macOS a plain `pip install` is blocked anyway.

```bash
pipx install git+https://github.com/darcodev/email-cleaner.git
```

Or with plain pip:

```bash
pip install git+https://github.com/darcodev/email-cleaner.git
```

To pin a specific released version, add the tag:

```bash
pipx install "git+https://github.com/darcodev/email-cleaner.git@v0.4.0"
```

Prefer to work from a clone?

```bash
git clone https://github.com/darcodev/email-cleaner.git
cd email-cleaner
pip install .
```

Or skip installing entirely and run `python -m email_cleaner` from inside the
repo folder.

If you do not have pipx yet: `python -m pip install --user pipx` then
`python -m pipx ensurepath` (reopen your terminal afterwards). To remove the
tool later: `pipx uninstall email-cleaner`.

## Setup

The tool needs two things: your email address and an app password (not your
normal login password - see the [next section](#app-passwords)). The easiest
way to give it those is a `.env` file.

```bash
cp .env.example .env      # on Windows: copy .env.example .env
```

Then open `.env` and fill it in:

```ini
EMAIL_CLEANER_EMAIL=you@gmail.com
EMAIL_CLEANER_PASSWORD=your-16-char-app-password
```

Run the tool from the folder that holds `.env` and it is read automatically.
To change your password later, just edit the file. `.env` is gitignored, so it
never gets committed.

You do not have to use a file. The same names work as real environment
variables, which is handy for a server or a scheduled job. Every setting can
come from four places, and this is the order of priority (first one wins):

1. A command line flag (`--email`, `--host`, `--port`).
2. A real environment variable.
3. The `.env` file.
4. A built-in default for your provider.

The full list of settings:

| Variable | What it is | Default |
| --- | --- | --- |
| `EMAIL_CLEANER_EMAIL` | The address to clean | Required |
| `EMAIL_CLEANER_PASSWORD` | Your app password | Asked for at runtime if unset |
| `EMAIL_CLEANER_HOST` | IMAP server hostname | Detected from your address for known providers |
| `EMAIL_CLEANER_PORT` | IMAP port | 993 |

If you leave the password out, the tool asks for it each run. It is shown as
you type (not hidden), so you can check it before pressing Enter.

The IMAP server is detected automatically from your address for these
providers:

| Provider | Detected for | IMAP host | Trash folder |
| --- | --- | --- | --- |
| Gmail | gmail.com, googlemail.com | imap.gmail.com | [Gmail]/Trash |
| Outlook | outlook.com, hotmail.com, live.com, msn.com | outlook.office365.com | Deleted |
| Yahoo | yahoo.com, ymail.com | imap.mail.yahoo.com | Trash |
| iCloud | icloud.com, me.com, mac.com | imap.mail.me.com | Deleted Messages |

For anything else (a work address, a custom domain), set `EMAIL_CLEANER_HOST`
in `.env` or pass `--host mail.example.com`. The Trash folder is then found
automatically; if that fails, pass `--trash-folder`.

## App passwords

Your normal account password will not work over IMAP. Providers require an
"app password" (sometimes called an app-specific password): a separate, revocable
password that only works for mail apps and does not unlock the rest of your
account. This is a good thing for a tool like this - if you ever want to cut off
access, you revoke that one password and nothing else is affected.

| Provider | Where to create one | Notes |
| --- | --- | --- |
| Gmail | https://myaccount.google.com/apppasswords | Needs 2-Step Verification turned on first |
| Yahoo | Account Security > Generate app password | |
| iCloud | https://appleid.apple.com/account/manage > App-Specific Passwords | |
| Outlook | https://account.live.com/proofs/apppassword | Microsoft is phasing these out; basic-auth IMAP may be disabled on your account |

Gmail shows the password as four groups of four letters. You can paste it with
or without the spaces; both work.

## Usage

Run it with no arguments and it walks you through a short menu: what kind of
mail (promotions, a specific category, or everything), how far back to go, and
any keywords or senders to narrow by. It then previews the matches and asks
before touching anything.

```bash
email-cleaner            # or: python -m email_cleaner
```

The menu only appears in a real terminal. If input or output is piped or
redirected (a cron job, `email-cleaner | tee log.txt`), it skips the menu and
falls back to a read-only scan so it never hangs waiting for an answer.

Prefer to skip the menu? There are three subcommands, and they all take the
same filters directly:

| Command | What it does |
| --- | --- |
| `scan` | Preview what would be cleaned. Read-only, changes nothing. |
| `clean` | Move the matching mail to Trash (asks first). |
| `unsubscribe` | List the unsubscribe links for the senders that mail you most. |

```bash
email-cleaner scan                    # read-only preview, no menu
email-cleaner clean                   # move promos older than 30 days to Trash
email-cleaner clean --yes             # same, but skip the confirmation prompt
email-cleaner unsubscribe             # print unsubscribe links, noisiest first
```

`scan` is the default when you pass filters without a subcommand, so
`email-cleaner --older-than 2y` behaves like `email-cleaner scan --older-than 2y`
and is always safe.

Run `email-cleaner clean --help` for the complete flag list.

## Filters in detail

Filters decide which messages match. The defaults are deliberately cautious:
promotional mail only, older than 30 days, in your inbox, skipping anything
starred or important.

| Flag | Meaning |
| --- | --- |
| `--older-than AGE` | Only mail older than this. See the age syntax below. Default `30d`. |
| `--keyword WORD` | Only mail containing this word or phrase. Repeatable. |
| `--from SENDER` | Only mail from this address or domain. Repeatable. |
| `--category NAME` | A Gmail category: `promotions` (default), `social`, `updates`, `forums`. Repeatable. |
| `--all` | Do not limit to promotional mail; match anything your other filters allow. |
| `--protect SENDER` | Never touch mail from this address or domain. Repeatable. Always wins. |
| `--include-starred` | Also match starred and important mail (skipped by default). |
| `--limit N` | Stop after this many matches (keeps the newest, cleans the oldest). |
| `--folder NAME` | Which folder to search. Default `INBOX`. |

Age syntax accepts a number plus a unit: `d` for days, `m` for months, `y` for
years. So `30d`, `3m`, `2y`. A bare number means days (`45` is `45d`). Months
are counted as 30 days and years as 365, so `3m` is 90 days and `2y` is 730 -
close enough for "old mail", not calendar-exact. Use `0` (or `all` / `any`) to
turn the age filter off entirely.

A few rules worth knowing:

- Repeated `--keyword` or `--from` filters match if ANY of them hit (it is an
  OR, not an AND).
- `--protect` is checked on every message on your side, even if the server
  search matched it, so a protected sender is never cleaned.
- Categories only apply on Gmail. On other servers the tool detects promotional
  mail a different way (see [below](#how-it-decides-what-is-promotional)).

## The three ways to delete

By default `clean` moves mail to Trash, which is reversible. There are two
stronger modes for when you actually want the space back. The difference
matters, especially on Gmail, so here is exactly what each one does.

| Mode | What happens | Recoverable? |
| --- | --- | --- |
| `clean` (default) | Copies the matches to your Trash folder and removes them from the inbox. | Yes, for about 30 days in Trash. |
| `clean --empty-trash` | Does the move above, then empties your entire Trash folder. | No. Anything already in Trash is deleted too. |
| `clean --permanent` | Marks the matches deleted in place and purges those exact messages, skipping Trash. | No. |

A Gmail note: on Gmail, deleting a message from a folder in plain IMAP only
removes that label; the message survives in "All Mail". The reliable way to
truly delete on Gmail and reclaim storage is `clean --empty-trash`, which moves
the mail to Trash and then empties it. Both `--empty-trash` and `--permanent`
make you type `yes` in full before anything is destroyed.

## How it decides what is promotional

On Gmail the tool uses Gmail's own search over IMAP (`category:promotions` plus
your other filters), which is the same classification as the Promotions tab, so
it is accurate. On any other IMAP server there is no such category, so it
instead matches mail that carries a `List-Unsubscribe` header - the standard
header that bulk and marketing senders are required to include. That is a good
proxy for "this is a mailing-list message" and rarely catches personal mail.

`--all` turns this promotional detection off and matches everything your other
filters allow, which is useful for jobs like "delete every message from this one
sender" regardless of whether it looks promotional.

Starred and important mail is always skipped unless you pass `--include-starred`,
and protected senders are re-checked on your machine for every message, even if
the server search returned them.

## AI-assisted filtering (optional)

The normal filters (age, keyword, sender, category, unsubscribe header) are fast
and predictable, but blunt: they cannot tell a shipping notification apart from a
"40% off shipping supplies" ad. An optional AI pass can. You describe in plain
language what you want gone, and a model makes a keep-or-delete call on each
message. It is **off by default** and turns on only when you pass `--ai`.

Two things make it safe to bolt onto a tool that deletes mail:

- **It can only narrow, never widen.** The model only ever decides keep-or-delete
  among the messages your normal filters already matched. Worst case, it keeps
  mail that would otherwise be trashed. It can never reach for a message the
  base filters did not select.
- **It fails safe.** If the backend is unreachable, times out, or returns
  something that will not parse, those messages default to *keep*. An AI outage
  can only ever cause fewer deletions, never more. Protected and starred mail is
  filtered out on your machine *before* anything is sent to the model, and is
  never deleted regardless of what the model says.

Run it read-only first, exactly like a normal scan:

```bash
email-cleaner scan --ai --ai-prompt "marketing and newsletters, but keep orders, flights and money"
email-cleaner scan --ai --ai-explain    # also show the model's one-line reason per match
```

Add `--ai-explain` to see, per message, why the model wants to drop it, so you
can sanity-check the judgment before running `clean`.

### Backends

You need a model to talk to. Point the tool at one with `EMAIL_CLEANER_AI_BACKEND`
(or `--ai-backend`). There are no Python packages to install for any of these -
the tool speaks plain HTTP+JSON to whatever you point it at.

| Backend | What it is | Data leaves your machine? |
| --- | --- | --- |
| `ollama` | A local [Ollama](https://ollama.com) daemon (default `http://localhost:11434`). You install and run Ollama and pull a model yourself. | **No.** This is the recommended, private option. |
| `openai` | Any endpoint speaking the OpenAI `/v1/chat/completions` shape - OpenAI itself, most hosted providers, or a compatible local server. Needs an API key. | Yes, to that provider. |
| `anthropic` | The Anthropic Messages API. Needs an API key. | Yes, to Anthropic. |

The local Ollama path is the one to reach for first: nothing about your mail
goes anywhere it does not already go. Set it up once:

```bash
# 1. install Ollama (see ollama.com), then pull a small model
ollama pull llama3.1
# 2. tell email-cleaner to use it (in .env, or as flags)
EMAIL_CLEANER_AI_BACKEND=ollama
EMAIL_CLEANER_AI_PROMPT=marketing and newsletters, but keep orders, flights and money
# 3. preview
email-cleaner scan --ai
```

For a hosted backend, set the key and the tool warns you (and, under `clean`,
asks) before any mail leaves the machine:

```bash
EMAIL_CLEANER_AI_BACKEND=openai
EMAIL_CLEANER_AI_API_KEY=sk-...
email-cleaner scan --ai --ai-prompt "junk, but not receipts"
```

### AI settings

Same pattern as the rest of the tool: environment variable (or `.env`) as the
default, a CLI flag to override per run. Nothing here is required unless you
opt in with `--ai`.

| Variable | Flag | What it is |
| --- | --- | --- |
| `EMAIL_CLEANER_AI_BACKEND` | `--ai-backend` | `ollama`, `openai`, or `anthropic`. Unset means the feature is off. |
| `EMAIL_CLEANER_AI_PROMPT` | `--ai-prompt` | The plain-language rule for what to delete. |
| `EMAIL_CLEANER_AI_MODEL` | `--ai-model` | Model id (e.g. `llama3.1`, `gpt-4o-mini`, `claude-haiku-4-5`). Defaults per backend. |
| `EMAIL_CLEANER_AI_API_KEY` | - | Key for the hosted backends. Required for `openai` / `anthropic`. |
| `EMAIL_CLEANER_AI_HOST` | - | Base URL, for a custom or self-hosted endpoint. Defaults per backend. |
| - | `--ai-explain` | Show the model's one-line reason for each match it keeps for deletion. |
| - | `--ai-snippet` | Also send a short slice of each message body to the model (see below). |

By default only the sender, subject, and whether a message carries an
unsubscribe header are sent - never the body. `--ai-snippet` additionally sends a
short, bounded slice of each message's first body part (a few hundred
characters, never the whole body, never attachments) for better judgment. It is
the one place the tool looks past headers, so it is behind its own flag.

Cost, for hosted backends: the model only ever runs on the already-filtered
candidate set (the promos that matched), not your whole mailbox, and the
messages are batched. Use `--limit` to cap it while you are dialling in a prompt.

## Recipes

Concrete tasks, copy-paste ready. Swap `clean` for `scan` first to preview any
of them safely.

```bash
# Preview the default cleanup without changing anything
email-cleaner scan

# Promotions older than two years
email-cleaner clean --older-than 2y

# Every message from a specific sender, any age
email-cleaner clean --from doordash.com --older-than 0

# Two food-delivery senders at once (matches either)
email-cleaner clean --from doordash.com --from ubereats.com --older-than 1y

# Deal mail matching certain words
email-cleaner clean --keyword sale --keyword "50% off" --keyword clearance

# Clean the Gmail Social and Updates tabs, not just Promotions
email-cleaner clean --category social --category updates

# Clean everything old, but never touch a few important senders
email-cleaner clean --all --older-than 1y --protect amazon.com --protect boss@work.com

# Try it on a small batch first
email-cleaner clean --limit 100

# Search beyond the inbox
email-cleaner clean --folder "[Gmail]/All Mail" --from oldnavy.com

# Let a local model decide keep-or-delete, previewing its reasons first
email-cleaner scan --ai --ai-explain --ai-prompt "newsletters and deals, but keep receipts"

# Export unsubscribe links for your noisiest senders to a file
email-cleaner unsubscribe --output links.txt

# Delete for good and reclaim the space (Gmail-friendly)
email-cleaner clean --older-than 2y --empty-trash
```

## Best practices

- Scan before you clean. `scan` is read-only; run it with the exact filters you
  plan to use, look at the top-senders table and the count, then rerun as
  `clean`.
- Start narrow. Add `--limit 100` the first time so you are reviewing a small,
  reversible batch before running it across the whole mailbox.
- Protect what matters. List important senders or whole domains with `--protect`
  (for example `--protect bank.com --protect boss@work.com`). Protection is
  enforced on every message, so it is the safe way to carve out exceptions.
- Leave `clean` in its default Trash mode unless you have a reason not to. Trash
  is recoverable for about 30 days, which is your safety net. Only reach for
  `--empty-trash` or `--permanent` once you trust your filters.
- Keep app passwords tight. Create one just for this tool, and revoke it in your
  provider's security settings when you are done or if you stop using the tool.
- Keep `.env` out of version control. It already is (the repo gitignores it),
  but if you copy the project around, do not copy `.env` into a shared or
  committed location.
- For a recurring job, use explicit flags plus `--yes` rather than the menu, so
  it never waits for input. See the next section.

## Automating it

Because the menu is skipped when input is not a terminal, you can run a fixed
cleanup on a schedule. Use `clean --yes` with explicit filters so it does not
stop to ask.

On Linux or macOS with cron (weekly, Sunday 3am):

```cron
0 3 * * 0 EMAIL_CLEANER_EMAIL=you@gmail.com EMAIL_CLEANER_PASSWORD=xxxx /path/to/email-cleaner clean --older-than 30d --yes
```

On Windows, create a Task Scheduler action that runs `email-cleaner` with the
arguments `clean --older-than 30d --yes`, with the two `EMAIL_CLEANER_*`
variables set in the task's environment.

For an unattended job, prefer real environment variables over a `.env` file, and
give the account an app password you can revoke independently.

## Safety and privacy

- `scan` never changes anything. It is safe to run as often as you like.
- `clean` moves mail to Trash and asks first. Your provider keeps Trash for
  about 30 days, so mistakes are recoverable.
- `clean --permanent` and `clean --empty-trash` are the destructive modes and
  make you type `yes` in full before deleting.
- Starred/important mail and `--protect` senders are always skipped.
- Your credentials are read only from `.env` or the environment and are used
  only to log in to your own IMAP server over TLS. They are never sent anywhere
  else.
- Nothing you clean is logged or written to disk. The tool keeps no history of
  what it touched. The single exception is `unsubscribe --output`, which writes
  the file you explicitly ask for.
- The optional AI pass (`--ai`) is the one feature that can send mail off your
  machine, and only with a *hosted* backend. A local Ollama backend sends
  nothing anywhere. A hosted backend (`openai` / `anthropic`) sends the sender,
  subject, and unsubscribe flag of each *matching* message - and a short body
  snippet only if you pass `--ai-snippet` - to that provider for classification.
  The tool prints a warning naming the provider before any of that leaves, and
  under `clean` it asks you to confirm (respecting `--yes`). It never sends your
  app password, full message bodies, or attachments, and it never persists
  prompts or responses. When the backend is unset, none of this applies.

See [SECURITY.md](SECURITY.md) for the full security model and how to report a
problem.

## Troubleshooting

The tool prints a plain error and a hint instead of a traceback for anything you
can fix yourself. The common ones:

- "Login failed for ...". Almost always the password. Use an app password, not
  your normal login password, and make sure 2-Step Verification is on (Gmail).
  Outlook may have basic-auth IMAP disabled on the account entirely.
- "Could not reach host:port". Check your internet connection and that the host
  name is right. For a custom server, confirm `EMAIL_CLEANER_HOST`.
- "TLS handshake with ... failed". The server may not use implicit TLS on this
  port. Most IMAP servers use 993; try that with `--port 993`.
- "Could not open folder '...'". The folder name does not exist on your server.
  List-style names differ between providers (for example `[Gmail]/All Mail`).
  Pass an existing one with `--folder`.
- "Could not find your Trash folder". Auto-detection did not find it, usually on
  a non-standard or non-English server. Pass it with `--trash-folder`.
- "No email address configured". Set `EMAIL_CLEANER_EMAIL` in `.env` or pass
  `--email`.
- "Don't know the IMAP server for ...". Your provider is not one of the built-in
  four. Set `EMAIL_CLEANER_HOST` or pass `--host`.
- "IMAP port '...' isn't a number". Fix `EMAIL_CLEANER_PORT`; it should be a
  number like 993.
- "Could not understand age '...'". Use a number plus `d`, `m` or `y` (like
  `30d`, `3m`, `2y`), or `0` for no age limit.

Asked for a password even though it is in `.env`? The file is read once at
startup, so an edit made while the tool is already running is not picked up -
just run it again. Also make sure you are running it from the folder that holds
`.env`, and that the line is `EMAIL_CLEANER_PASSWORD=...` with a value after the
`=`.

## Exit codes

Useful if you script it:

| Code | Meaning |
| --- | --- |
| 0 | Success, or nothing matched. |
| 1 | You declined the confirmation prompt; nothing was changed. |
| 2 | An error you can fix (bad setting, login failure, missing folder). |
| 130 | Interrupted with Ctrl-C. |

## How it works

It is a thin wrapper over Python's standard-library `imaplib`. A run does four
things: resolve your account, search the folder, fetch just the headers of the
matches, and (for `clean`) move them.

- Everything works in IMAP UIDs rather than sequence numbers, so the mailbox
  shifting underneath the run can never make it act on the wrong message.
- The search happens on the server. On Gmail that is Gmail's own query engine
  (via the `X-GM-RAW` extension); elsewhere it is a standard IMAP `SEARCH`.
- Only message headers are fetched (sender, subject, date, size, flags,
  unsubscribe header), never message bodies, and they are fetched in batches to
  keep it quick on large mailboxes.
- Moving to Trash uses the IMAP `MOVE` extension when the server supports it
  (Gmail does), which is a single round trip per batch; otherwise it falls back
  to copy, mark deleted, then purge.
- Purging names the exact UIDs it is removing (`UID EXPUNGE`, RFC 4315). A plain
  `EXPUNGE` would take out every message in the folder that anything had ever
  flagged deleted, including mail another client left in that state. On the rare
  server without `UIDPLUS` there is no scoped option and the run says so.

The source is small and split by job: `cli.py` (arguments and commands),
`config.py` (account and `.env`), `providers.py` (provider presets),
`scanner.py` (filters and search), `imap_client.py` (the IMAP session),
`ui.py` (terminal output), and `errors.py`.

## Contributing

Bug reports and pull requests are welcome. The short version: the project is
standard-library only (please keep it dependency-free), and it ships with a test
suite that runs on Python 3.9 through 3.13 in CI.

```bash
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the details.

## License

MIT, see [LICENSE](LICENSE).
