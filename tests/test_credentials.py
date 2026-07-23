from __future__ import annotations

import pytest

from nvidia_nim_proxy.credentials import (
    API_KEY_MODE_CLIENT,
    API_KEY_MODE_ENV,
    API_KEY_MODE_POOL,
    CredentialBroker,
    extract_bearer_token,
    fingerprint_secret,
    load_pool_keys,
)


def test_extract_bearer_token_is_case_insensitive() -> None:
    assert extract_bearer_token("Bearer client-secret") == "client-secret"
    assert extract_bearer_token("bearer client-secret") == "client-secret"
    assert extract_bearer_token("Basic client-secret") is None
    assert extract_bearer_token(None) is None


def test_secret_fingerprint_is_stable_and_does_not_expose_secret() -> None:
    fingerprint = fingerprint_secret("client-secret")

    assert fingerprint == fingerprint_secret("client-secret")
    assert len(fingerprint) == 12
    assert "client-secret" not in fingerprint


def test_load_pool_keys_orders_numeric_suffixes_and_ignores_unrelated_values() -> None:
    environ = {
        "NVIDIA_API_KEY_10": "key-ten",
        "NVIDIA_API_KEY_2": "key-two",
        "NVIDIA_API_KEY_1": "key-one",
        "NVIDIA_API_KEY": "legacy-key",
        "UNRELATED": "ignored",
    }

    assert load_pool_keys(environ) == ("key-one", "key-two", "key-ten")


def test_load_pool_keys_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate NVIDIA API key"):
        load_pool_keys(
            {
                "NVIDIA_API_KEY_1": "same-secret",
                "NVIDIA_API_KEY_2": "same-secret",
            }
        )


def test_load_pool_keys_rejects_blank_or_missing_pool() -> None:
    with pytest.raises(ValueError, match="NVIDIA_API_KEY_1 is empty"):
        load_pool_keys({"NVIDIA_API_KEY_1": "  "})

    with pytest.raises(ValueError, match="at least one numbered NVIDIA API key"):
        load_pool_keys({"NVIDIA_API_KEY": "legacy-only"})


def test_env_and_client_modes_resolve_direct_nvidia_keys() -> None:
    env_broker = CredentialBroker(
        mode=API_KEY_MODE_ENV,
        env_api_key="env-secret",
        local_client_key=None,
    )
    client_broker = CredentialBroker(
        mode=API_KEY_MODE_CLIENT,
        env_api_key=None,
        local_client_key=None,
    )

    assert env_broker.resolve_direct_key("Bearer ignored-client-key") == "env-secret"
    assert client_broker.resolve_direct_key("Bearer client-secret") == "client-secret"


def test_pool_auth_uses_local_key_but_never_returns_it_as_upstream_key() -> None:
    broker = CredentialBroker(
        mode=API_KEY_MODE_POOL,
        env_api_key=None,
        local_client_key="local-only-secret",
    )

    broker.authorize_pool_client("Bearer local-only-secret")

    with pytest.raises(ValueError, match="not available in pool mode"):
        broker.resolve_direct_key("Bearer local-only-secret")


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic local-only-secret", "Bearer wrong-secret"],
)
def test_pool_auth_rejects_missing_or_incorrect_local_key(
    authorization: str | None,
) -> None:
    broker = CredentialBroker(
        mode=API_KEY_MODE_POOL,
        env_api_key=None,
        local_client_key="local-only-secret",
    )

    with pytest.raises(PermissionError, match="invalid local proxy bearer token"):
        broker.authorize_pool_client(authorization)


def test_broker_rejects_unknown_mode_and_missing_mode_credentials() -> None:
    with pytest.raises(ValueError, match="unsupported API key mode"):
        CredentialBroker(mode="unknown", env_api_key=None, local_client_key=None)

    with pytest.raises(ValueError, match="missing NVIDIA_API_KEY"):
        CredentialBroker(
            mode=API_KEY_MODE_ENV,
            env_api_key=None,
            local_client_key=None,
        ).resolve_direct_key(None)

    with pytest.raises(PermissionError, match="missing client bearer token"):
        CredentialBroker(
            mode=API_KEY_MODE_CLIENT,
            env_api_key=None,
            local_client_key=None,
        ).resolve_direct_key(None)
