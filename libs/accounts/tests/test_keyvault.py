"""Key Vault secret loading (D92): fixed hosts, checked names, fail closed, no leaks."""

from __future__ import annotations

import json
import urllib.request

import pytest
from vitalmesh_accounts.keyvault import SecretUnavailable, fetch_secret, parse_reference
from vitalmesh_accounts.tokens import MIN_SECRET_BYTES, secret_from_env

TOKEN = "tok-" + "t" * 40
SECRET = "s" * 48


class FakeAzure:
    """Stands in for the metadata service and the vault; records every request."""

    def __init__(self, secret: str = SECRET, fail_at: int | None = None) -> None:
        self.requests: list[urllib.request.Request] = []
        self.secret, self.fail_at = secret, fail_at

    def __call__(self, req: urllib.request.Request, timeout: float) -> bytes:
        self.requests.append(req)
        if self.fail_at == len(self.requests):
            raise OSError(f"connection to {req.full_url} with {TOKEN} refused")
        if "169.254.169.254" in req.full_url:
            return json.dumps({"access_token": TOKEN}).encode()
        return json.dumps({"value": self.secret}).encode()


def test_reads_the_secret_with_the_vm_identity() -> None:
    az = FakeAzure()
    assert fetch_secret("vmbenchabc123/auth-secret", az) == SECRET
    token_req, secret_req = az.requests
    assert token_req.full_url.startswith("http://169.254.169.254/metadata/identity/oauth2/token")
    assert token_req.get_header("Metadata") == "true"
    assert secret_req.full_url == (
        "https://vmbenchabc123.vault.azure.net/secrets/auth-secret?api-version=7.4"
    )
    assert secret_req.get_header("Authorization") == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "novault",
        "evil.com/secret",
        "vault.evil.com/x",
        "VAULT/x",
        "ab/x",
        "a" * 25 + "/x",
        "vaultname/../x",
        "vaultname/a b",
        "vaultname/",
        "vaultname/x?api=1",
        "vaultname@evil/x",
        "https://vaultname/x",
    ],
)
def test_bad_references_never_reach_the_network(ref: str) -> None:
    az = FakeAzure()
    with pytest.raises(SecretUnavailable):
        fetch_secret(ref, az)
    assert az.requests == []


def test_failure_message_leaks_neither_token_nor_url() -> None:
    az = FakeAzure(fail_at=2)
    with pytest.raises(SecretUnavailable) as err:
        fetch_secret("vmbenchabc123/auth-secret", az)
    assert TOKEN not in str(err.value) and "https://" not in str(err.value)
    assert "OSError" in str(err.value)


def test_an_empty_secret_is_an_error() -> None:
    with pytest.raises(SecretUnavailable, match="empty"):
        fetch_secret("vmbenchabc123/auth-secret", FakeAzure(secret=""))


def test_parse_reference_splits_on_the_first_slash() -> None:
    assert parse_reference(" vmbenchabc123/auth-secret ") == ("vmbenchabc123", "auth-secret")


def test_the_signing_secret_comes_from_the_vault_when_asked() -> None:
    env = {"VITALMESH_AUTH_SECRET_KEYVAULT": "vmbenchabc123/auth-secret"}
    assert secret_from_env(env, FakeAzure()) == SECRET.encode()


def test_a_plain_environment_secret_wins_over_the_vault() -> None:
    az = FakeAzure()
    env = {
        "VITALMESH_AUTH_SECRET": "e" * MIN_SECRET_BYTES,
        "VITALMESH_AUTH_SECRET_KEYVAULT": "vmbenchabc123/auth-secret",
    }
    assert secret_from_env(env, az) == b"e" * MIN_SECRET_BYTES
    assert az.requests == []


def test_a_vault_failure_stops_the_process_instead_of_using_a_random_secret() -> None:
    env = {"VITALMESH_AUTH_SECRET_KEYVAULT": "vmbenchabc123/auth-secret"}
    with pytest.raises(SecretUnavailable):
        secret_from_env(env, FakeAzure(fail_at=1))


def test_a_short_vault_secret_is_rejected_like_a_short_env_secret() -> None:
    env = {"VITALMESH_AUTH_SECRET_KEYVAULT": "vmbenchabc123/auth-secret"}
    with pytest.raises(ValueError, match="at least"):
        secret_from_env(env, FakeAzure(secret="too short"))


def test_the_real_opener_refuses_other_schemes() -> None:
    from vitalmesh_accounts.keyvault import _urlopen

    for url in ("file:///etc/passwd", "ftp://example.org/x", "http://example.org/x"):
        with pytest.raises(SecretUnavailable, match="unexpected address"):
            _urlopen(urllib.request.Request(url), 1.0)
