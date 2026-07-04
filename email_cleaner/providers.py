"""IMAP presets for the common providers: host, trash folder name, and
where to go to create an app password."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str
    host: str
    port: int
    trash_folder: str | None
    app_password_url: str | None
    note: str


PROVIDERS: dict[str, Provider] = {
    "gmail": Provider(
        key="gmail",
        host="imap.gmail.com",
        port=993,
        trash_folder="[Gmail]/Trash",
        app_password_url="https://myaccount.google.com/apppasswords",
        note="Requires 2-Step Verification, then create an app password.",
    ),
    "outlook": Provider(
        key="outlook",
        host="outlook.office365.com",
        port=993,
        trash_folder="Deleted",
        app_password_url="https://account.live.com/proofs/apppassword",
        note="Microsoft is phasing out app passwords for personal accounts; "
        "if login fails, IMAP basic auth may be disabled for your account.",
    ),
    "yahoo": Provider(
        key="yahoo",
        host="imap.mail.yahoo.com",
        port=993,
        trash_folder="Trash",
        app_password_url="https://login.yahoo.com/account/security",
        note="Create an app password under Security > Generate app password.",
    ),
    "icloud": Provider(
        key="icloud",
        host="imap.mail.me.com",
        port=993,
        trash_folder="Deleted Messages",
        app_password_url="https://appleid.apple.com/account/manage",
        note="Create an app-specific password in your Apple ID settings.",
    ),
    "custom": Provider(
        key="custom",
        host="",
        port=993,
        trash_folder=None,
        app_password_url=None,
        note="Pass --host or set EMAIL_CLEANER_HOST. Trash folder is auto-detected.",
    ),
}


def get_provider(key: str) -> Provider:
    try:
        return PROVIDERS[key.lower()]
    except KeyError:
        valid = ", ".join(PROVIDERS)
        raise ValueError(f"Unknown provider '{key}'. Valid options: {valid}") from None


def guess_provider(email_address: str) -> Provider | None:
    """Best-effort provider detection from the email domain."""
    domain = email_address.rsplit("@", 1)[-1].lower()
    # not exhaustive, but covers most people. everyone else can pass --provider
    mapping = {
        "gmail.com": "gmail",
        "googlemail.com": "gmail",
        "outlook.com": "outlook",
        "hotmail.com": "outlook",
        "live.com": "outlook",
        "msn.com": "outlook",
        "yahoo.com": "yahoo",
        "ymail.com": "yahoo",
        "icloud.com": "icloud",
        "me.com": "icloud",
        "mac.com": "icloud",
    }
    key = mapping.get(domain)
    return PROVIDERS[key] if key else None
