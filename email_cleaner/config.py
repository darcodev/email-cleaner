"""Account resolution. The easy path is a .env file next to the project with
EMAIL_CLEANER_EMAIL / EMAIL_CLEANER_PASSWORD in it (see .env.example); we also
read the plain enviroment and the matching CLI flags. _HOST / _PORT are only
needed for servers we can't guess. We only ever read the .env - nothing about
what you clean is written to disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import ui
from .ai import DEFAULT_HOSTS, DEFAULT_MODELS, VALID_BACKENDS, AISettings
from .errors import CleanerError
from .providers import Provider, get_provider, guess_provider

ENV_EMAIL = "EMAIL_CLEANER_EMAIL"
ENV_PASSWORD = "EMAIL_CLEANER_PASSWORD"
ENV_HOST = "EMAIL_CLEANER_HOST"
ENV_PORT = "EMAIL_CLEANER_PORT"

ENV_AI_BACKEND = "EMAIL_CLEANER_AI_BACKEND"
ENV_AI_MODEL = "EMAIL_CLEANER_AI_MODEL"
ENV_AI_API_KEY = "EMAIL_CLEANER_AI_API_KEY"
ENV_AI_HOST = "EMAIL_CLEANER_AI_HOST"
ENV_AI_PROMPT = "EMAIL_CLEANER_AI_PROMPT"


def _find_dotenv() -> Path:
    # current folder first (that's where people drop it), then the config dir
    # so a saved one keeps working no matter where you run from. first hit wins
    candidates = [Path.cwd() / ".env", Path.home() / ".email-cleaner" / ".env"]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _clean_value(value: str) -> str:
    """Tidy the right-hand side of a KEY=VALUE line.

    A quoted value keeps everything between the quotes verbatim, so a '#' or
    spaces in a password survive. An unquoted value has a trailing inline
    '# comment' stripped, matching how people annotate a .env.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    marker = value.find(" #")  # inline comment must have space before the hash
    if marker != -1:
        value = value[:marker]
    return value.strip()


def load_dotenv(path: Path | None = None) -> None:
    """Pull KEY=VALUE lines out of a .env file into os.environ.

    Not a real dotenv library, we dont need one - just enough to read the
    handful of EMAIL_CLEANER_* keys. Handles blank lines, full-line and inline
    # comments, a stray 'export ' prefix, optional quotes around the value, and
    a UTF-8 BOM (Windows editors like Notepad add one). Anything already set in
    the real environment wins and is left alone.
    """
    path = path or _find_dotenv()
    if not path.exists():
        return
    # utf-8-sig drops a leading BOM if there is one, so the first key doesn't
    # come through with a stray BOM prefix and then get looked up wrong
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):  # tolerate a bash-style line if pasted
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _clean_value(value)
        # rebuild the list of what's already set each time - tiny file, dont care
        already_set = [name for name in os.environ]
        if key and key not in already_set:
            os.environ[key] = value


@dataclass
class Account:
    address: str
    password: str
    host: str
    port: int
    provider_key: str
    trash_folder: str | None


def resolve_account(args) -> Account:
    # pull in the .env file if there is one, then: flags beat env beat defaults
    load_dotenv()
    address = getattr(args, "email", None) or os.environ.get(ENV_EMAIL)
    if not address:
        raise CleanerError(
            "No email address configured.",
            hint=(
                "Copy .env.example to .env and fill it in (see the README), "
                "or pass --email."
            ),
        )

    provider: Provider | None = None
    provider_key = getattr(args, "provider", None)
    if provider_key:
        provider = get_provider(provider_key)
    else:
        provider = guess_provider(address)

    host = (
        getattr(args, "host", None)
        or os.environ.get(ENV_HOST)
        or (provider.host if provider else "")
    )
    if not host:
        raise CleanerError(
            f"Don't know the IMAP server for {address}.",
            hint=f"Pass --host mail.example.com or set {ENV_HOST}.",
        )
    port_raw = (
        getattr(args, "port", None)
        or os.environ.get(ENV_PORT)
        or (provider.port if provider else 993)
    )
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        # easy to fat-finger this one in .env
        raise CleanerError(
            f"IMAP port '{port_raw}' isn't a number.",
            hint=f"Fix {ENV_PORT} in your .env - it should be a port like 993.",
        ) from None

    password = os.environ.get(ENV_PASSWORD)
    if not password:
        # nothing in the env, so ask. Shown, not hidden, so you can eyeball it.
        if provider and provider.app_password_url:
            ui.info(provider.note)
            ui.info(f"Create one here: {provider.app_password_url}")
        password = ui.prompt(f"App password for {address}")
    if not password:
        raise CleanerError(
            "No password provided.",
            hint=f"Put {ENV_PASSWORD} in your .env file, or type it when asked.",
        )

    trash = getattr(args, "trash_folder", None) or (
        provider.trash_folder if provider else None
    )

    return Account(
        address=address,
        password=password,
        host=host,
        port=port,
        provider_key=provider.key if provider else "custom",
        trash_folder=trash,
    )


def resolve_ai_settings(args) -> AISettings | None:
    """Resolve the optional AI classification wiring, same flags-beat-env-beat-
    defaults order as the account. Returns None when the feature is off (no
    --ai), so a normal run behaves exactly as it does today."""
    if not getattr(args, "ai", False):
        return None

    load_dotenv()  # idempotent; resolve_account may already have run it

    backend = (
        getattr(args, "ai_backend", None) or os.environ.get(ENV_AI_BACKEND) or ""
    ).strip().lower()
    if not backend:
        raise CleanerError(
            "AI classification is on but no backend is set.",
            hint=(
                f"Set {ENV_AI_BACKEND} to one of {', '.join(VALID_BACKENDS)}, "
                "or pass --ai-backend."
            ),
        )
    if backend not in VALID_BACKENDS:
        raise CleanerError(
            f"Unknown AI backend '{backend}'.",
            hint=f"Valid backends: {', '.join(VALID_BACKENDS)}.",
        )

    prompt = (
        getattr(args, "ai_prompt", None) or os.environ.get(ENV_AI_PROMPT) or ""
    ).strip()
    if not prompt:
        raise CleanerError(
            "AI classification is on but no rule was given.",
            hint=(
                'Describe what to delete with --ai-prompt "..." or set '
                f"{ENV_AI_PROMPT}."
            ),
        )

    model = (
        getattr(args, "ai_model", None)
        or os.environ.get(ENV_AI_MODEL)
        or DEFAULT_MODELS[backend]
    )
    host = os.environ.get(ENV_AI_HOST) or DEFAULT_HOSTS[backend]
    api_key = os.environ.get(ENV_AI_API_KEY)
    if backend in ("openai", "anthropic") and not api_key:
        raise CleanerError(
            f"The {backend} AI backend needs an API key.",
            hint=f"Set {ENV_AI_API_KEY} in your .env - hosted backends require a key.",
        )

    return AISettings(
        backend=backend,
        model=model,
        host=host,
        api_key=api_key,
        prompt=prompt,
        snippet=getattr(args, "ai_snippet", False),
    )
