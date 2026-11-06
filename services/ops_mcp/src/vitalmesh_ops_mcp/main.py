"""Entry point: ``uv run python -m vitalmesh_ops_mcp`` serves the ops tools over stdio (D88).

An MCP client starts this process itself (it opens no port). Configuration, all from the
environment and never printed:

    VITALMESH_OPS_EMAIL / VITALMESH_OPS_PASSWORD   an account with the operator role
    VITALMESH_API_URL                              default http://127.0.0.1:8000

Create the account with ``VITALMESH_NEW_PASSWORD=... uv run python scripts/create_account.py
<email> --role operator``. Without credentials the server still starts and every tool says it is
not configured.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from vitalmesh_ops_mcp.client import HttpOpsApi, OpsError
from vitalmesh_ops_mcp.server import build_server


class NotConfigured:
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        raise OpsError(
            "Not configured: set VITALMESH_OPS_EMAIL and VITALMESH_OPS_PASSWORD (an operator "
            "account) in the environment of this server."
        )


def main() -> None:
    email = os.environ.get("VITALMESH_OPS_EMAIL", "")
    password = os.environ.get("VITALMESH_OPS_PASSWORD", "")
    api: Any
    if email and password:
        api = HttpOpsApi(
            os.environ.get("VITALMESH_API_URL", "http://127.0.0.1:8000"), email, password
        )
    else:
        print("vitalmesh-ops: no operator credentials in the environment", file=sys.stderr)
        api = NotConfigured()
    build_server(api).run(transport="stdio")


if __name__ == "__main__":
    main()
