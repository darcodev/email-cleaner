# Contributing

Thanks for taking a look. Bug reports, ideas, and pull requests are all welcome.

## Getting started

There is nothing to install beyond Python 3.9 or newer - the project uses only
the standard library, and that is a deliberate constraint (see the guidelines
below).

```bash
git clone https://github.com/darcodev/email-cleaner.git
cd email-cleaner
python -m email_cleaner --help
```

You can run the tool straight from the source tree with `python -m email_cleaner`,
so you do not need to install it to try a change.

## Running the tests

The test suite covers the pure logic (filter building, age parsing, header
parsing, provider detection, config loading) and needs no network or real
mailbox.

```bash
python -m unittest discover -s tests -v
```

CI runs the same suite on Python 3.9, 3.10, 3.11, 3.12 and 3.13. A pull request
needs it green.

## Project layout

The code is small and split by job:

| File | Responsibility |
| --- | --- |
| `cli.py` | Argument parsing, the three commands, and the interactive menu. |
| `config.py` | Resolving the account and reading `.env`. |
| `providers.py` | Built-in host / port / Trash presets per provider. |
| `scanner.py` | Turning filters into a search and collecting the results. |
| `imap_client.py` | The IMAP session wrapper (search, fetch, move, delete). |
| `ui.py` | Terminal output: colors, tables, prompts, progress. |
| `errors.py` | `CleanerError`, for problems the user can fix. |

## Guidelines

- Keep it dependency-free. The tool should run with a bare Python install and
  nothing from PyPI. If a change seems to need a third-party package, open an
  issue to discuss it first.
- Match the surrounding style. Plain, readable code and comments; no clever
  one-liners for their own sake.
- Keep user-facing text plain ASCII (help text, prompts, error messages, docs).
  It avoids encoding problems on older Windows consoles.
- Add a test for any logic change, and make sure the whole suite passes.
- Errors the user can fix should raise `CleanerError` with a short message and a
  `hint`, so they see a friendly line instead of a traceback.

## Submitting a change

1. Fork the repo and create a branch for your change.
2. Make the change and add or update tests.
3. Run the test suite locally.
4. Open a pull request describing what it does and why.

For anything security-related, please do not open a public issue - see
[SECURITY.md](SECURITY.md).
