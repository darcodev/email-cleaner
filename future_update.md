# Future update: AI-assisted filtering

This started as a design note for an optional AI classification step and is now
**implemented** (module `email_cleaner/ai.py`, wired through `config.py`,
`scanner.py`, `cli.py`, with tests in `tests/test_units.py` and docs in the
README and SECURITY.md). The note is kept as the record of what it should do,
what it must not do, and how it fits the existing code; the "Decisions made
while building" section at the end captures where the shipped version settled
the open questions and where it deviated from the original sketch.

## The idea in one line

Let the user describe in plain language what they want gone ("newsletters and
marketing, but keep anything about my orders, flights or money") and have a
model decide keep-or-delete per message, run either against a local model
through Ollama or a hosted model through an API key.

## Why bother

The current filters (age, keyword, from, Gmail category, List-Unsubscribe) are
fast and predictable, but they are blunt. They cannot tell a shipping
notification apart from a "40% off shipping supplies" ad, and they cannot act on
intent like "junk, but not receipts". A model can, at the cost of speed, money
and, for hosted models, privacy. So this is strictly opt-in and layered on top
of the existing filters, never a replacement for the cautious defaults.

## Design principles (do not break these)

These match the promises the tool already makes. The AI feature has to keep all
of them or it does not ship.

1. Zero third-party packages. The whole appeal is "just the standard library".
   That rules out the `openai`, `anthropic` and `requests` packages. Both
   Ollama and every hosted chat API are plain HTTP+JSON, so `urllib.request` and
   `json` are enough. No SDKs.
2. Off by default. A run with no AI flags behaves exactly as it does today. The
   feature only turns on when the user sets a backend and asks for it.
3. It can only narrow, never widen. The model decides keep-or-delete among the
   messages the normal filters already matched. Worst case it keeps mail that
   would otherwise be trashed. It never reaches for a message the base filters
   did not select. (An explicit override to let it widen is an open question
   below, and would default off.)
4. The existing safety nets still run after it. Protected senders and
   starred/important mail are re-checked on our side regardless of what the
   model said, same as today in `scan()`.
5. Fail safe. If the backend is unreachable, times out, returns junk, or the
   response will not parse, that message defaults to KEEP. An AI outage must
   never cause a message to be deleted. It can only ever cause fewer deletions.
6. No new disk writes. The tool keeps no history and writes nothing about what
   it saw. That still holds. No caching classifications to disk, no logging
   prompts or responses. The one existing exception (`unsubscribe --output`)
   stays the only thing that touches the filesystem.
7. Say plainly when data leaves the machine. Today nothing about your mail goes
   anywhere except your own IMAP server. A hosted backend breaks that, so it
   gets a loud, explicit warning (see Privacy).

## Two backends

Both speak HTTP+JSON, so one small client can cover them with different base
URLs and auth.

- `ollama` (local, default recommendation): talks to a local Ollama daemon,
  default `http://localhost:11434`. Nothing leaves the machine. The user has to
  install and run Ollama themselves and pull a model; that is their setup, not a
  Python dependency of ours. This is the privacy-preserving path and should be
  the one the docs lead with.
- `openai` (hosted, OpenAI-compatible): talks to any endpoint that implements
  the OpenAI `/v1/chat/completions` shape. That one code path covers OpenAI
  itself, most of the hosted providers, and even Ollama's own compatibility
  endpoint. Needs an API key. This is the "via API keys" path.

Anthropic's Messages API has a slightly different request/response shape. It is
worth supporting as a third backend (`anthropic`) since it is a natural fit, but
it is a separate small adapter rather than the same code path. Treat it as a
follow-on to the two above, not a blocker.

## Configuration

Same pattern as the rest of the tool: environment variable (or `.env`) as the
default, CLI flag to override per run. Nothing here is required unless the user
opts in.

| Variable | Meaning | Default |
| --- | --- | --- |
| `EMAIL_CLEANER_AI_BACKEND` | `ollama`, `openai`, `anthropic`, or unset | unset (off) |
| `EMAIL_CLEANER_AI_MODEL` | model id, e.g. `llama3.1`, `gpt-4o-mini`, `claude-haiku-4-5` | per-backend default |
| `EMAIL_CLEANER_AI_API_KEY` | key for hosted backends | none (required for hosted) |
| `EMAIL_CLEANER_AI_HOST` | base URL, for Ollama or a custom endpoint | `http://localhost:11434` for ollama |
| `EMAIL_CLEANER_AI_PROMPT` | default plain-language rule | none |

CLI flags on the shared filter group in `cli.py`:

- `--ai` turn the classification pass on for this run.
- `--ai-prompt "..."` the plain-language rule; overrides `EMAIL_CLEANER_AI_PROMPT`.
- `--ai-backend`, `--ai-model` per-run overrides of the env vars.
- `--ai-explain` in `scan` output, show the model's one-line reason per message
  it wants to drop, so the user can sanity-check the judgment before cleaning.

If `--ai` is set with no backend resolved, that is a `CleanerError` with a hint
pointing at `EMAIL_CLEANER_AI_BACKEND`, matching how the account errors read.

## Where it slots into the code

The pipeline today (in `scanner.py`) is: build a server-side query, search,
`fetch_summaries`, then a client-side pass that drops protected and starred mail
and returns a `ScanResult`. The AI step goes in as one more stage in that
client-side pass, after the base filters have already narrowed things down:

```
search -> fetch_summaries -> protect/starred re-check -> [AI classify] -> ScanResult
```

(The original sketch put the AI step *before* the protect/starred re-check. The
shipped order runs the safety net first, so protected and starred mail is never
sent to the model at all - strictly better for privacy and hosted-backend cost,
and it changes no safety guarantee. See the decisions section.)

Running AI on the already-narrowed candidate set (not the whole mailbox) is what
bounds both cost and risk. You are asking the model about the hundreds of promo
messages that matched, not your tens of thousands of total messages.

Concretely:

- New module `email_cleaner/ai.py`. A small `Classifier` with backend adapters,
  a `classify(emails, prompt) -> dict[uid, Verdict]` method, and the `urllib`
  HTTP plumbing. `Verdict` carries keep/delete plus an optional short reason.
- New fields on `Filters`: `ai_enabled`, `ai_prompt` (plus backend/model, or
  resolve those in `config.py` into an `AISettings` object the way `Account` is
  resolved). Leaning toward a separate `AISettings` so `Filters` stays about
  what-to-match and the AI wiring stays in `config.py`.
- `scan()` gains an optional classifier argument. When present, after
  `fetch_summaries` it calls `classify`, then keeps only messages whose verdict
  is delete (defaulting unknown/None verdicts to keep). `ScanResult` grows a
  `skipped_ai` counter so `_show_report` can say "kept N that the model judged
  not junk", the same way it already reports protected and starred skips.
- `ui.py` gets the `--ai-explain` rendering. No other module changes.

Keeping it to one new module plus small edits in `scanner.py`/`config.py`/`cli.py`
mirrors how the code is already split by job.

## The classification call

- Batch messages per request (say 20-50 subjects+senders) rather than one call
  per message, to cut round trips and cost. Chunk the candidate list the way
  `imap_client` already chunks UIDs.
- Send the minimum that lets the model judge: sender display name, sender
  address, subject, and whether a `List-Unsubscribe` header was present. That is
  all already in `EmailSummary`; no extra IMAP fetch needed.
- Ask for strict JSON back: a list of `{uid, action, reason}` where action is
  `keep` or `delete`. Parse defensively. Any uid missing from the response, any
  unparseable row, any non-JSON reply -> that message defaults to keep. Log
  nothing.
- Timeout every request (reuse the 30s style from `ImapSession.connect`). On
  timeout or transport error, that whole batch defaults to keep and the run
  continues; surface a single "AI backend unreachable, kept those messages"
  note rather than aborting the clean.
- Progress callback like the existing `on_progress(done, total)` so the scan
  shows "Classifying 120/340" the way header fetching does.

## Optional: a body snippet

The tool fetches headers only today and the README makes a point of it. Better
classification would come from a short body snippet, but fetching bodies is a
real departure from that promise. So: keep headers-only as the default, and put
snippet fetching behind an explicit `--ai-snippet` flag with its own note in the
docs. If enabled, fetch a small bounded slice of the text part (a few hundred
characters), never the whole body, never attachments.

## Privacy (the part that matters most)

This is the one feature that can send your mail off your machine, so it needs to
be impossible to do by accident.

- Ollama and any localhost backend: nothing leaves the machine. This is the
  recommended default and the docs should say so up front.
- Hosted backend: the sender, subject and unsubscribe flag of each candidate
  message (and the body snippet, if `--ai-snippet` is on) are sent to that
  provider. That is a genuine change from the tool's current "your credentials
  and mail never go anywhere but your own IMAP server" guarantee.
  - Print a clear one-time warning before the first hosted call in a run,
    naming the provider host, e.g. "AI backend is api.openai.com. Subjects and
    senders of the matching messages will be sent there for classification."
  - Under `clean`, require confirmation of that (respect `--yes` for
    automation, like the other prompts).
  - Document it in README Safety and privacy and in SECURITY.md as an explicit,
    opt-in exception to the data-never-leaves rule.
- Never send the app password, full bodies (unless `--ai-snippet`), or
  attachments. Never persist prompts or responses.

## Cost

Hosted backends cost money per token. Mitigations, in order: run only on the
already-filtered candidate set; batch; send headers not bodies; let the user cap
with the existing `--limit`. Worth printing a rough "about to classify N
messages" line before a hosted run so a big mailbox is not a surprise bill.

## Decisions made while building

These settle the open questions the note left, plus the one deviation from the
sketch.

- **Widen? No.** The model still only ever narrows the base-filter matches.
  There is no widen flag; if it is ever added it needs its own flag and a scary
  confirmation, and it fights principle 3.
- **Safety net runs before the AI pass, not after.** The sketch classified
  first, then re-checked protect/starred. The shipped `scan()` re-checks
  protect/starred first and classifies only the survivors. Same safety (those
  messages are never deletable either way), but a hosted backend never sees
  protected or starred mail, which is better for privacy and cost. Principle 4
  ("safety nets still run") holds: nothing the model says can delete protected
  or starred mail.
- **All three backends shipped in the first cut.** `ollama`, `openai`, and
  `anthropic` are all in `ai.py`; the anthropic adapter turned out small enough
  (a different URL, `x-api-key`/`anthropic-version` headers, and a `content[]`
  reply shape) that holding it back as a follow-up was not worth it.
- **Default model ids** are pinned per backend (`llama3.1`, `gpt-4o-mini`,
  `claude-haiku-4-5`) and documented as drift-prone and overridable via
  `EMAIL_CLEANER_AI_MODEL` / `--ai-model`.
- **Reason text** shows as a truncated "Why (AI)" column in the `scan`/`clean`
  preview table, only under `--ai-explain`. The existing `ui.table` already
  squeezes the widest column to fit the terminal, so the model's short reason
  slots in without a separate renderer.
- **"Data leaves" is decided by the host, not the backend name.** Whether the
  privacy warning fires is based on whether the resolved host is localhost, so
  pointing the `openai` backend at a local Ollama is correctly treated as local
  and stays quiet.

## Still open / possible follow-ups

- Menu integration: the interactive menu does not offer the AI pass yet; it is a
  flags/env feature for now. Adding a menu step is a clean future addition.
- The `--ai-snippet` body slice is best effort - it grabs a bounded slice of the
  first body part and strips obvious HTML, but does not decode transfer
  encodings (quoted-printable/base64), so some snippets will be less clean than
  others. Good enough to help the model; worth revisiting if it proves noisy.

## Rough plan

1. `ai.py`: backend adapters (ollama, openai) over `urllib`, `classify()` with
   batching, strict JSON parsing, fail-safe-to-keep, timeouts.
2. `config.py`: resolve `AISettings` from env/flags, with clear errors.
3. `scanner.py`: thread the classifier through `scan()`, add `skipped_ai` to
   `ScanResult`, default unknown verdicts to keep.
4. `cli.py`: `--ai`, `--ai-prompt`, `--ai-backend`, `--ai-model`, `--ai-explain`,
   `--ai-snippet`; the hosted-backend warning and confirmation.
5. `ui.py`: report AI skips and the `--ai-explain` reasons.
6. Docs: a README section leading with the Ollama/local path, the privacy
   exception in README and SECURITY.md, `.env.example` entries.
7. Tests: JSON parsing (including malformed responses default to keep), batching
   and chunk boundaries, backend selection, fail-safe on transport error, and
   that an unreachable backend never deletes. Mock the HTTP layer so the suite
   stays offline and dependency-free.

## Non-goals

- Not replacing the existing filters. AI is an extra pass, not the new default.
- No SDKs, no new runtime dependencies.
- No auto-unsubscribe or any action the model triggers on its own. It only
  classifies; the user still previews and confirms the clean.
- No storing or learning from past runs. Every run is stateless, same as now.
