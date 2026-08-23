from __future__ import annotations


class CleanerError(Exception):
    """Anything the user can fix themselves. The CLI catches these and
    prints the message (plus an optional hint) instead of a traceback."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class SearchUnsupported(CleanerError):
    """The server rejected a SEARCH outright (an IMAP BAD reply) instead of
    running it and returning no matches. Yahoo answers HEADER searches this
    way. The scanner catches this to fall back to filtering on our side; if
    nothing does, it still prints like any other CleanerError.
    """


class ConnectionLost(CleanerError):
    """The server hung up mid-command (an IMAP BYE / imaplib abort).

    Recoverable in a way most errors are not: every pass re-runs its own
    search, so nothing is half-done that a reconnect cannot simply redo.
    Yahoo drops long sessions like this once a run has moved a few hundred
    messages, which is exactly when giving up would waste the most work.
    """
