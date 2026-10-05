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


class _MockCreds:
    def __init__(self, valid=True, expired=False, refresh_token="tok", can_refresh=True):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.can_refresh = can_refresh
        self.refreshed = False

    def refresh(self, request):
        if not self.can_refresh:
            raise RuntimeError("refresh failed")
        self.refreshed = True
        self.valid = True
        self.expired = False

    def to_json(self):
        return '{"token": "refreshed"}'


class TestCredentialsHelpers:
    def test_is_credentials_valid_when_valid(self):
        creds = _MockCreds(valid=True)
        assert auth.is_credentials_valid(creds) is True
        assert creds.refreshed is False

    def test_is_credentials_valid_refreshes_when_expired(self):
        creds = _MockCreds(valid=False, expired=True, refresh_token="tok")
        assert auth.is_credentials_valid(creds) is True
        assert creds.refreshed is True

    def test_is_credentials_valid_returns_false_on_refresh_failure(self):
        creds = _MockCreds(valid=False, expired=True, refresh_token="tok", can_refresh=False)
        assert auth.is_credentials_valid(creds) is False

    def test_save_token_file(self, tmp_path):
        target = tmp_path / "sub" / "token.json"
        creds = _MockCreds()
        auth.save_token_file(creds, str(target))
        assert target.exists()
        assert target.read_text(encoding="utf-8") == '{"token": "refreshed"}'


class TestEnsureCredentials:
    def test_valid_token_file_returned(self, tmp_path, monkeypatch):
        token_path = tmp_path / "token.json"
        token_path.write_text('{"token": "ok"}', encoding="utf-8")
        creds = _MockCreds(valid=True)
        monkeypatch.setattr(auth, "load_token_file", lambda p: creds)

        res = auth.ensure_credentials(str(tmp_path / "client_secret.json"), token_path=str(token_path))
        assert res is creds

    def test_missing_client_secret_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            auth.ensure_credentials(str(tmp_path / "nonexistent.json"), token_path=str(tmp_path / "no_token.json"))

    def test_non_interactive_raises_runtime_error(self, tmp_path):
        cs = make_client_secret(tmp_path)
        with pytest.raises(RuntimeError) as exc_info:
            auth.ensure_credentials(cs, token_path=str(tmp_path / "no_token.json"), interactive=False)
        assert "token.json 不存在或已失效" in str(exc_info.value)

    def test_interactive_triggers_oauth_flow(self, tmp_path, monkeypatch):
        cs = make_client_secret(tmp_path)
        mock_creds = _MockCreds()
        called = {}

        def mock_flow(client_secret_path, token_path=None, **kwargs):
            called["client"] = client_secret_path
            called["token"] = token_path
            return mock_creds

        monkeypatch.setattr(auth, "headless_oauth_flow", mock_flow)
        res = auth.ensure_credentials(cs, token_path=str(tmp_path / "token.json"), interactive=True)
        assert res is mock_creds
        assert called["client"] == os.path.abspath(cs)
