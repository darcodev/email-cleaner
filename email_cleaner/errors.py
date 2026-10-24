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

    A new search can usually resume the cleanup. Copy-based moves are an
    exception: the copy may have landed while the source is still present, so
    retrying could create duplicates. Those losses have retry_safe=False.
    """

    def __init__(self, message: str, hint: str | None = None, *, retry_safe: bool = True):
        super().__init__(message, hint=hint)
        self.retry_safe = retry_safe
