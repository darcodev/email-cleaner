"""Optional AI keep-or-delete pass.

This is strictly opt-in and layered on top of the normal filters. The model
only ever decides keep-or-delete among the messages the base filters already
matched, so at worst it keeps mail that would otherwise be trashed - it can
never reach for a message the filters did not select.

Everything here is fail-safe: if the backend is unreachable, times out, returns
junk, or the reply will not parse, the affected messages default to KEEP. An AI
outage can only ever cause fewer deletions, never more.

No third-party packages - both Ollama and every hosted chat API are plain
HTTP+JSON, so urllib and json are enough. Three backends share most of the code:
  - ollama    : a local Ollama daemon (nothing leaves the machine)
  - openai    : any endpoint speaking the OpenAI /v1/chat/completions shape
  - anthropic : the Anthropic Messages API (a small separate adapter)
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlparse

# Timeout per request, matching the 30s style ImapSession.connect uses. A slow
# or wedged backend must not hang the whole run - it times out and we keep.
AI_TIMEOUT = 30

# How many messages we describe to the model in one request. Batching cuts
# round trips and, on hosted backends, cost. Kept small enough that the JSON
# reply stays well within a cheap model's output budget.
AI_BATCH = 25

VALID_BACKENDS = ("ollama", "openai", "anthropic")

# Per-backend default model ids. These drift, so they are easy to override with
# EMAIL_CLEANER_AI_MODEL / --ai-model. The anthropic default is the cheap/fast
# tier, matching how the openai default is a small model.
DEFAULT_MODELS = {
    "ollama": "llama3.1",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}

# Per-backend default base URL. Ollama talks to the local daemon; the hosted
# ones point at their public API. All three are overridable via
# EMAIL_CLEANER_AI_HOST for self-hosted or compatible endpoints.
DEFAULT_HOSTS = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}

# Max reply length for the anthropic backend (the only one that requires it).
# ~25 short {uid, action, reason} rows fit comfortably; give generous headroom.
_ANTHROPIC_MAX_TOKENS = 2048

# Hostnames that mean "this machine", so nothing leaves it.
LOCAL_HOSTNAMES = ("localhost", "127.0.0.1", "::1")


@dataclass
class Verdict:
    """The model's call on one message. `delete` is only ever acted on when
    True; anything we could not parse never becomes a Verdict at all and so
    defaults to keep upstream."""

    delete: bool
    reason: str = ""


@dataclass
class AISettings:
    """Resolved AI wiring. Built in config.py from env vars and CLI flags so
    Filters stays about what-to-match and this stays about how-to-classify."""

    backend: str
    model: str
    host: str
    api_key: str | None
    prompt: str
    snippet: bool = False

    @property
    def is_local(self) -> bool:
        """True when the backend lives on this machine, so no mail leaves it.
        Based on the host, not the backend name, so pointing the openai backend
        at a local Ollama is correctly treated as local.

        A host we can't read (no scheme, so urlparse finds no hostname at all -
        'api.openai.com/v1' parses that way) counts as remote, not local: the
        wrong answer here silently skips the privacy warning and the consent
        prompt, so it has to fail towards asking. config.resolve_ai_settings
        rejects those up front; this is the backstop."""
        hostname = urlparse(self.host).hostname
        if not hostname:
            return False
        return hostname.lower() in LOCAL_HOSTNAMES

    @property
    def is_hosted(self) -> bool:
        return not self.is_local

    @property
    def provider_host(self) -> str:
        """Host name to name in the privacy warning."""
        return urlparse(self.host).hostname or self.host


class _BackendError(Exception):
    """Anything that went wrong talking to a backend - unreachable, timeout,
    bad status, unparseable response. Caught inside classify() and turned into
    a keep, never propagated as a crash."""


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _loads_lenient(raw: str):
    """Parse JSON from a model reply, tolerating prose or ``` fences around it.

    Returns the parsed value, or None if nothing JSON-shaped is in there. We try
    a straight parse first, then fall back to the widest {...} or [...] span.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass
    # Models sometimes wrap the JSON in prose or ``` fences. Build the widest
    # span for each bracket type and try the earliest-starting one first, so a
    # wrapped array like [ {..} ] is not mistaken for its first { .. } element
    # (which would then miss the "results" key and drop every verdict).
    spans = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            spans.append((start, raw[start : end + 1]))
    for _, span in sorted(spans):
        try:
            return json.loads(span)
        except (ValueError, TypeError):
            continue
    return None


def _parse_verdicts(raw: str, batch: list) -> dict[str, Verdict]:
    """Turn a model reply into {uid: Verdict}. Defensive by design: any row we
    cannot read, and any uid not in this batch, is skipped. A uid missing from
    the reply is simply absent, so it defaults to keep upstream. Only the exact
    string "delete" deletes; every other action keeps."""
    valid = {e.uid for e in batch}
    data = _loads_lenient(raw)
    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}
    out: dict[str, Verdict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = str(row.get("uid", "")).strip()
        if uid not in valid:
            continue
        action = str(row.get("action", "")).strip().lower()
        reason = str(row.get("reason", "")).strip()
        out[uid] = Verdict(delete=(action == "delete"), reason=reason)
    return out


def _extract_text(backend: str, payload) -> str | None:
    """Pull the assistant's text out of a backend's JSON response shape."""
    try:
        if backend == "ollama":
            return payload["message"]["content"]
        if backend == "openai":
            return payload["choices"][0]["message"]["content"]
        if backend == "anthropic":
            # content is a list of blocks; take the first text block
            for block in payload.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
            return None
    except (KeyError, IndexError, TypeError):
        return None
    return None


class Classifier:
    """Runs the keep-or-delete pass over a list of EmailSummary objects.

    classify() batches them, asks the backend for strict JSON, parses it
    defensively, and returns {uid: Verdict}. Transport failures are swallowed
    into keeps and noted once in `transport_error` so the CLI can surface a
    single "backend unreachable, kept those messages" line.
    """

    def __init__(self, settings: AISettings, timeout: int = AI_TIMEOUT, batch_size: int = AI_BATCH):
        self.settings = settings
        self.timeout = timeout
        self.batch_size = batch_size
        # set on the first failed batch; the CLI reads it after the scan
        self.transport_error: str | None = None

    @property
    def wants_snippet(self) -> bool:
        return self.settings.snippet

    def classify(
        self,
        emails: list,
        snippets: dict[str, str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Verdict]:
        snippets = snippets or {}
        results: dict[str, Verdict] = {}
        total = len(emails)
        done = 0
        for batch in _chunks(emails, self.batch_size):
            try:
                raw = self._call(batch, snippets)
                results.update(_parse_verdicts(raw, batch))
            except _BackendError as exc:
                # fail safe: leave this batch unjudged, so every message in it
                # defaults to keep. Note it once for the CLI to report.
                if self.transport_error is None:
                    self.transport_error = (
                        f"{self.settings.backend} AI backend unreachable ({exc})"
                    )
            done += len(batch)
            if on_progress:
                on_progress(min(done, total), total)
        return results

    # -- prompt construction -------------------------------------------------

    def _system_prompt(self) -> str:
        return (
            "You are an email triage assistant. You will be given a numbered list "
            "of emails (sender, subject, and whether they carry an unsubscribe "
            "link). Decide, for each one, whether it matches this rule for "
            "deletion:\n\n"
            f"{self.settings.prompt}\n\n"
            'Reply with ONLY a JSON object of the form {"results": [{"uid": '
            '"<uid>", "action": "delete" | "keep", "reason": "<short>"}]}. '
            "Include one entry per email using the exact uid given. \"delete\" "
            "means the email matches the rule and should be removed; \"keep\" "
            "means it does not. Keep each reason under 10 words. When unsure, "
            "choose keep. Output nothing but the JSON."
        )

    def _user_content(self, batch: list, snippets: dict[str, str]) -> str:
        lines = []
        for i, e in enumerate(batch, 1):
            unsub = "yes" if e.unsubscribe else "no"
            lines.append(
                f"{i}) uid={e.uid} | from: {e.sender_display} | "
                f"subject: {e.subject} | unsubscribe: {unsub}"
            )
            snippet = snippets.get(e.uid)
            if snippet:
                lines.append(f"   snippet: {snippet}")
        return "\n".join(lines)

    # -- backend adapters ----------------------------------------------------

    def _build_request(self, batch: list, snippets: dict[str, str]) -> tuple[str, dict, dict]:
        """Return (url, headers, body) for the configured backend."""
        backend = self.settings.backend
        system = self._system_prompt()
        user = self._user_content(batch, snippets)
        base = self.settings.host.rstrip("/")

        if backend == "ollama":
            url = f"{base}/api/chat"
            headers = {"Content-Type": "application/json"}
            body = {
                "model": self.settings.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "format": "json",  # ollama can constrain output to JSON
                "options": {"temperature": 0},
            }
            return url, headers, body

        if backend == "openai":
            url = f"{base}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.api_key}",
            }
            body = {
                "model": self.settings.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            return url, headers, body

        if backend == "anthropic":
            url = f"{base}/v1/messages"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.settings.api_key or "",
                "anthropic-version": "2023-06-01",
            }
            body = {
                "model": self.settings.model,
                "max_tokens": _ANTHROPIC_MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            return url, headers, body

        # config.resolve_ai_settings validates the backend, so this is a bug
        raise _BackendError(f"unknown backend '{backend}'")

    def _call(self, batch: list, snippets: dict[str, str]) -> str:
        url, headers, body = self._build_request(batch, snippets)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # a bad status (401 wrong key, 404 wrong model, 5xx) - keep, and let
            # the note carry the code so the user can tell auth from an outage
            raise _BackendError(f"{exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise _BackendError(str(getattr(exc, "reason", exc) or exc)) from exc

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise _BackendError("response was not JSON") from exc

        text = _extract_text(self.settings.backend, payload)
        if text is None:
            raise _BackendError("response had no content")
        return text
