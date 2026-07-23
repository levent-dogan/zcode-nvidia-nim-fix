"""Secret-safe credential handling for proxy API key modes."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Mapping


API_KEY_MODE_ENV = "env"
API_KEY_MODE_CLIENT = "client"
API_KEY_MODE_POOL = "pool"
API_KEY_MODES = frozenset(
    {
        API_KEY_MODE_ENV,
        API_KEY_MODE_CLIENT,
        API_KEY_MODE_POOL,
    }
)
_POOL_KEY_PATTERN = re.compile(r"^NVIDIA_API_KEY_([1-9][0-9]*)$")


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Extract a bearer token without logging or validating token content."""

    if authorization_header is None:
        return None

    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    token = parts[1].strip()
    return token or None


def fingerprint_secret(secret: str) -> str:
    """Return a stable short fingerprint without exposing the secret."""

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def load_pool_keys(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Load numbered NVIDIA keys in numeric order and reject unsafe pools."""

    numbered_keys: list[tuple[int, str, str]] = []
    for name, raw_value in environ.items():
        match = _POOL_KEY_PATTERN.fullmatch(name)
        if match is None:
            continue

        value = raw_value.strip()
        if not value:
            raise ValueError(f"{name} is empty")
        numbered_keys.append((int(match.group(1)), name, value))

    if not numbered_keys:
        raise ValueError("pool mode requires at least one numbered NVIDIA API key")

    numbered_keys.sort(key=lambda item: item[0])
    seen_fingerprints: set[str] = set()
    ordered_keys: list[str] = []
    for _, name, value in numbered_keys:
        fingerprint = fingerprint_secret(value)
        if fingerprint in seen_fingerprints:
            raise ValueError(f"duplicate NVIDIA API key detected at {name}")
        seen_fingerprints.add(fingerprint)
        ordered_keys.append(value)

    return tuple(ordered_keys)


@dataclass(frozen=True)
class CredentialBroker:
    """Resolve direct credentials or authorize access to the private key pool."""

    mode: str
    env_api_key: str | None
    local_client_key: str | None

    def __post_init__(self) -> None:
        if self.mode not in API_KEY_MODES:
            raise ValueError(f"unsupported API key mode: {self.mode}")

    def authorize_pool_client(self, authorization_header: str | None) -> None:
        """Validate the local pool credential using a constant-time comparison."""

        if self.mode != API_KEY_MODE_POOL:
            raise ValueError("pool authorization is only available in pool mode")

        supplied = extract_bearer_token(authorization_header)
        expected = self.local_client_key
        if (
            supplied is None
            or expected is None
            or not hmac.compare_digest(supplied, expected)
        ):
            raise PermissionError("invalid local proxy bearer token")

    def resolve_direct_key(self, authorization_header: str | None) -> str:
        """Resolve an NVIDIA key for env/client modes only."""

        if self.mode == API_KEY_MODE_CLIENT:
            client_token = extract_bearer_token(authorization_header)
            if client_token is None:
                raise PermissionError("missing client bearer token")
            return client_token

        if self.mode == API_KEY_MODE_ENV:
            if self.env_api_key is None or not self.env_api_key.strip():
                raise ValueError("missing NVIDIA_API_KEY")
            return self.env_api_key

        raise ValueError("direct NVIDIA key is not available in pool mode")
