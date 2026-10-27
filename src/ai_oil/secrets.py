"""Secret storage in the Windows Credential Manager through `keyring` (GOV-8). Values are never logged."""

import keyring
import keyring.errors

SERVICE = "ai_oil"


def set_secret(ref: str, value: str) -> None:
    """Store `value` under `ref`."""
    keyring.set_password(SERVICE, ref, value)


def get_secret(ref: str) -> str | None:
    """Return the secret stored under `ref`, or None if absent."""
    return keyring.get_password(SERVICE, ref)


def delete_secret(ref: str) -> None:
    """Remove the secret under `ref`; no error if it is absent."""
    try:
        keyring.delete_password(SERVICE, ref)
    except keyring.errors.PasswordDeleteError:
        pass
