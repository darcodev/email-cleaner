# Security

email-cleaner logs in to your mailbox, so it is worth being clear about how it
handles credentials and what it does and does not do with your data.

## Supported versions

This is a small project with a single active line. Security fixes go on the
latest release; please make sure you are on the newest version before reporting
an issue.

| Version | Supported |
| --- | --- |
| 0.4.x | Yes |
| < 0.4 | No |

## How your credentials are handled

- The tool reads your address and app password only from `.env` or from real
  environment variables. It does not prompt for or store them anywhere else.
- Those credentials are used for exactly one thing: logging in to your own IMAP
  server over TLS. They are never transmitted to any other host, and there is no
  telemetry, analytics, or "phone home" of any kind.
- Nothing about what you clean is written to disk. The tool keeps no history,
  log, or cache of the messages it touched. The only file it ever writes is the
  one you explicitly request with `unsubscribe --output`.
- `.env` is gitignored so it cannot be committed by accident.

## The AI pass and where your mail goes

The optional AI classification pass (`--ai`, off by default) is the one feature
that can send message details off your machine, so it is worth being explicit:

- It is opt-in per run and does nothing unless you both pass `--ai` and configure
  a backend (`EMAIL_CLEANER_AI_BACKEND`).
- With the **local** `ollama` backend, nothing leaves your machine. This is the
  recommended, privacy-preserving option.
- With a **hosted** backend (`openai` or `anthropic`), the sender, subject, and
  unsubscribe flag of each message that matched your filters - plus a short body
  snippet only if you pass `--ai-snippet` - are sent to that provider over TLS
  for classification. This is a deliberate, opt-in exception to the "your mail
  never leaves your own IMAP server" guarantee above. The tool prints a warning
  naming the provider before the first such request, and under `clean` requires
  confirmation (which `--yes` may skip for automation).
- The AI pass never sends your app password, full message bodies, or
  attachments, and it never persists prompts or responses to disk. If the
  backend fails or replies with junk, the affected messages default to *keep*, so
  an AI problem can only ever result in fewer deletions.

## Use an app password, not your real password

Always give the tool a provider "app password" (or app-specific password), never
your main account password. An app password only works for mail access and can
be revoked on its own, so cutting off this tool later is a single click in your
provider's security settings and does not affect anything else on your account.

If you stop using the tool, revoke the app password you created for it.

## Safe by default

- `scan` is read-only and changes nothing.
- `clean` moves mail to Trash (recoverable for about 30 days) and asks before it
  acts.
- The destructive modes, `clean --empty-trash` and `clean --permanent`, require
  you to type `yes` in full.
- Starred/important mail and any `--protect` senders are never touched.

## Reporting a vulnerability

Please report suspected security problems privately, not in a public issue. Use
GitHub's "Report a vulnerability" button under the repository's Security tab,
which opens a private advisory visible only to the maintainer.

When you report, include the version, your platform and Python version, and
steps to reproduce. You can expect an acknowledgement within a few days. Please
give a reasonable window to fix and release before any public disclosure.
