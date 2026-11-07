"""Read one secret from Azure Key Vault using the machine's own identity (D92).

On an Azure VM the instance metadata service (a link-local address that only the VM can reach)
hands out a short-lived token for the VM's managed identity; that token reads the secret from the
vault's REST endpoint. No password, key or connection string is stored anywhere, so the signing
secret no longer has to live in an environment file or ``.env``.

Rules this module keeps:

* The reference is ``<vault>/<secret>``. Both names are checked against Azure's own naming rules
  BEFORE they go into a URL, so a configuration value can never point the request at another host.
* The hosts are fixed: ``169.254.169.254`` for the token and ``<vault>.vault.azure.net`` for the
  secret. Timeouts are short and responses are size-capped.
* Neither the token nor the secret is ever logged, and error messages never include a response
  body (it could echo either). A failure raises ``SecretUnavailable``: the caller must stop, not
  fall back to a random or default secret, because a deployment that asked for a vault wants one.
* Standard library only, so the API gains no new dependency to audit.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable

IMDS_URL = (
    "http://169.254.169.254/metadata/identity/oauth2/token"
    "?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net"
)
VAULT_API = "7.4"
TIMEOUT_S = 5.0
MAX_BYTES = 64 * 1024
_VAULT = re.compile(r"^[a-z][a-z0-9-]{1,22}[a-z0-9]$")  # 3-24 characters, as Azure requires
_SECRET = re.compile(r"^[A-Za-z0-9-]{1,127}$")

Fetch = Callable[[urllib.request.Request, float], bytes]


class SecretUnavailable(RuntimeError):
    """The secret could not be read. The message never contains the token or the secret."""


def _urlopen(req: urllib.request.Request, timeout: float) -> bytes:
    # defence in depth: only the metadata address over http or an https URL, never file: or others
    if not req.full_url.startswith(("http://169.254.169.254/", "https://")):
        raise SecretUnavailable("refusing a request to an unexpected address")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - scheme checked above
        data: bytes = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise SecretUnavailable("the key vault answer was larger than expected")
    return data


def parse_reference(ref: str) -> tuple[str, str]:
    vault, sep, name = ref.strip().partition("/")
    if not sep or not _VAULT.match(vault) or not _SECRET.match(name):
        raise SecretUnavailable(
            "a key vault reference must look like <vault-name>/<secret-name> "
            "(lower-case vault name of 3-24 characters; secret name letters, digits, hyphens)"
        )
    return vault, name


def fetch_secret(ref: str, fetch: Fetch = _urlopen) -> str:
    vault, name = parse_reference(ref)
    try:
        token_req = urllib.request.Request(IMDS_URL, headers={"Metadata": "true"})
        token = json.loads(fetch(token_req, TIMEOUT_S))["access_token"]
        secret_req = urllib.request.Request(
            f"https://{vault}.vault.azure.net/secrets/{name}?api-version={VAULT_API}",
            headers={"Authorization": f"Bearer {token}"},
        )
        value = json.loads(fetch(secret_req, TIMEOUT_S))["value"]
    except SecretUnavailable:
        raise
    except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError) as exc:
        # the exception type is enough to debug; its text could echo a URL or a body
        raise SecretUnavailable(
            f"could not read secret {name!r} from vault {vault!r} ({type(exc).__name__}); "
            "is this an Azure VM with a managed identity that may read secrets?"
        ) from None
    if not isinstance(value, str) or not value:
        raise SecretUnavailable(f"secret {name!r} in vault {vault!r} is empty")
    return value
