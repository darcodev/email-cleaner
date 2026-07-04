from __future__ import annotations


class CleanerError(Exception):
    """Anything the user can fix themselves. The CLI catches these and
    prints the message (plus an optional hint) instead of a traceback."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint
