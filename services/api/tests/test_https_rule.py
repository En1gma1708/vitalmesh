"""An https address means Secure cookies; the mistaken combination is refused at start (D92)."""

from __future__ import annotations

import pytest
from vitalmesh_api.main import secure_cookies_from_env


def test_plain_http_local_default_is_not_secure() -> None:
    assert secure_cookies_from_env({}) is False


def test_https_base_url_turns_secure_cookies_on_by_itself() -> None:
    assert secure_cookies_from_env({"VITALMESH_BASE_URL": "https://app.example.org"}) is True
    assert secure_cookies_from_env({"VITALMESH_BASE_URL": "HTTPS://app.example.org"}) is True


def test_a_tls_terminating_proxy_can_force_it_on_with_the_flag() -> None:
    env = {"VITALMESH_BASE_URL": "http://10.0.0.5:8000", "VITALMESH_SECURE_COOKIES": "1"}
    assert secure_cookies_from_env(env) is True


def test_https_with_the_flag_off_is_refused() -> None:
    env = {"VITALMESH_BASE_URL": "https://app.example.org", "VITALMESH_SECURE_COOKIES": "0"}
    with pytest.raises(SystemExit, match="refusing to start"):
        secure_cookies_from_env(env)
