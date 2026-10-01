from __future__ import annotations

import json
import os

import pytest

from tubetape import auth


def test_oauthlib_scope_relaxed():
    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1"


def make_client_secret(tmp_path):
    data = {
        "installed": {
            "client_id": "x.apps.googleusercontent.com",
            "client_secret": "y",
            "redirect_uris": ["http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class TestHeadlessOAuthFlow:
    def test_prints_authorization_url(self, tmp_path):
        cs = make_client_secret(tmp_path)
        printed = []
        # input_fn returns a URL without a code, so it errors before any network.
        with pytest.raises(ValueError):
            auth.headless_oauth_flow(
                cs,
                input_fn=lambda _prompt: "http://localhost:8080/?state=x",
                print_fn=printed.append,
            )
        auth_url = [line for line in printed if line.startswith("https://")][0]
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080" in auth_url
        assert "access_type=offline" in auth_url

    def test_missing_code_raises(self, tmp_path):
        cs = make_client_secret(tmp_path)
        with pytest.raises(ValueError):
            auth.headless_oauth_flow(
                cs,
                input_fn=lambda _prompt: "http://localhost:8080/?state=x",
                print_fn=lambda _s: None,
            )
