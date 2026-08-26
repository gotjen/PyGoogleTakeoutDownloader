#!/usr/bin/env python3
"""
Shared credential storage/retrieval helpers.

Tries the OS keyring first for every credential; falls back to a plaintext
value in the loaded secrets.json config only when keyring is unavailable,
locked, or empty for that key. Import `keyring` only here so tests can mock
`credentials.keyring` in one place.
"""

import logging
from typing import Optional

try:
    import keyring
except ImportError:  # pragma: no cover - exercised via mocking
    keyring = None

DEFAULT_SERVICE = 'google_takeout'


def is_keyring_available() -> bool:
    return keyring is not None


def get_credential(key: str, config: dict, service: str = DEFAULT_SERVICE,
                    logger: Optional[logging.Logger] = None) -> Optional[str]:
    """
    Retrieve a credential, preferring the OS keyring over plaintext config.
    """
    logger = logger or logging.getLogger(__name__)

    if keyring is not None:
        try:
            value = keyring.get_password(service, key)
            if value:
                return value
        except Exception as e:
            logger.warning(f"Keyring retrieval failed for '{key}': {e}")

    value = (config or {}).get('google_takeout', {}).get(key)
    if value:
        logger.warning(
            f"Falling back to plaintext secrets.json for '{key}' — "
            "keyring unavailable or empty"
        )
    return value or None


def set_credential(key: str, value: str, service: str = DEFAULT_SERVICE,
                    logger: Optional[logging.Logger] = None) -> bool:
    """
    Store a credential in the OS keyring, verifying the write by reading it
    back. Returns False (without raising) if keyring is unavailable or the
    write can't be verified, so callers can decide on a plaintext fallback.
    """
    logger = logger or logging.getLogger(__name__)

    if keyring is None:
        return False

    try:
        keyring.set_password(service, key, value)
        if keyring.get_password(service, key) == value:
            return True
        logger.warning(f"Keyring write for '{key}' did not verify on read-back")
        return False
    except Exception as e:
        logger.warning(f"Keyring storage failed for '{key}': {e}")
        return False
