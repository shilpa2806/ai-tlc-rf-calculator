"""Small helpers for reading configuration from environment variables."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

_TRUTHY = {"true", "1", "t", "yes", "y", "on"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def env_bool(name: str, default: bool = False) -> bool:
    """Return an environment variable interpreted as a boolean.

    Args:
        name: The environment variable name.
        default: Value to return when the variable is unset.

    Returns:
        ``True`` if the value is one of ``true/1/t/yes/y/on`` (case-insensitive).
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def env_int(name: str, default: int) -> int:
    """Return an environment variable parsed as an int, or ``default`` if unset/invalid.

    Args:
        name: The environment variable name.
        default: Value to return when the variable is unset or not an integer.

    Returns:
        The parsed integer, or ``default``.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_list(name: str, default: str = "") -> list[str]:
    """Return a comma-separated environment variable as a list of trimmed items.

    Args:
        name: The environment variable name.
        default: Fallback raw string used when the variable is unset.

    Returns:
        The non-empty, whitespace-stripped items.
    """
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def csrf_origins_from_hosts(hosts: list[str]) -> list[str]:
    """Derive ``https://<host>`` CSRF origins from real ALLOWED_HOSTS entries.

    Local (``localhost``/``127.0.0.1``) and wildcard hosts are skipped, so the
    result is safe to use as a default for ``CSRF_TRUSTED_ORIGINS``.
    """
    return [f"https://{h}" for h in hosts if h not in _LOCAL_HOSTS and "*" not in h]


def host_of(url: str) -> str | None:
    """Return the hostname of a URL (e.g. ``https://eln.example.org/x`` -> host), or None."""
    return urlsplit(url).hostname if url else None


# Parts of Django that sign data with SECRET_KEY and so need the same key in
# every worker and across restarts (see SECRET_KEY in settings.py).
_SIGNING_APPS = {
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
}
_SIGNING_MIDDLEWARE = {
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
}


def signs_data(installed_apps: list[str], middleware: list[str]) -> bool:
    """Whether the configured apps or middleware sign data with SECRET_KEY."""
    return bool(_SIGNING_APPS & set(installed_apps) or _SIGNING_MIDDLEWARE & set(middleware))
