"""Secret storage in the Windows Credential Manager through `keyring` (GOV-8). Values are never logged."""

import keyring
import keyring.errors

from ai_oil.errors import ConfigError

SERVICE = "ai_oil"
_NO_STORE = (
    "no secure credential store is available ({error}) — on Windows the Credential Manager is used; "
    "on other systems install a keyring backend (GOV-8)"
)


def set_secret(ref: str, value: str) -> None:
    """Store `value` under `ref`."""
    try:
        keyring.set_password(SERVICE, ref, value)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(_NO_STORE.format(error=type(exc).__name__)) from exc


def get_secret(ref: str) -> str | None:
    """Return the secret stored under `ref`, or None if absent."""
    try:
        return keyring.get_password(SERVICE, ref)
    except keyring.errors.KeyringError as exc:
        raise ConfigError(_NO_STORE.format(error=type(exc).__name__)) from exc


def delete_secret(ref: str) -> None:
    """Remove the secret under `ref`; no error if it is absent."""
    try:
        keyring.delete_password(SERVICE, ref)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError as exc:
        raise ConfigError(_NO_STORE.format(error=type(exc).__name__)) from exc
