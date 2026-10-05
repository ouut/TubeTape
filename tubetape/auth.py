"""YouTube OAuth credential handling.

Credentials come from ``client_secret.json`` + ``token.json`` (or a token
string passed via environment). Scopes: ``youtube.upload`` and
``youtube.readonly``.
"""

from __future__ import annotations

import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .log import get_logger

_logger = get_logger("auth")

# Google may return more scopes than requested when re-authorizing with a
# superset (e.g. adding youtube.force-ssl on top of previously-granted
# scopes). oauthlib rejects that mismatch by default; relax it.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# youtube.force-ssl covers upload, delete (rebuild), playlist insert, and
# read. youtube.upload alone cannot delete videos or manage playlists.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

TOKEN_ENV = "TUBETAPE_TOKEN"


def credentials_from_token_string(token_str: str) -> Credentials:
    """Build credentials from a token JSON string (e.g. an env var)."""
    try:
        info = json.loads(token_str)
    except json.JSONDecodeError as exc:
        _logger.error("token string is not valid JSON")
        raise ValueError("token string is not valid JSON") from exc
    _logger.debug("built credentials from TUBETAPE_TOKEN env var")
    return Credentials.from_authorized_user_info(info, SCOPES)


def load_token_file(path: str) -> Credentials:
    """Load credentials from a token.json file."""
    try:
        creds = Credentials.from_authorized_user_file(path, SCOPES)
    except (OSError, ValueError) as exc:
        _logger.error("cannot load token file %s: %s", path, exc)
        raise ValueError(f"cannot load token file {path}: {exc}") from exc
    _logger.info("loaded YouTube credentials from %s", path)
    return creds


def is_credentials_valid(creds: Credentials) -> bool:
    """Return True if credentials are valid, attempting refresh if expired."""
    if creds.valid:
        return True
    if creds.expired and creds.refresh_token:
        try:
            _logger.info("refreshing expired OAuth token")
            creds.refresh(Request())
            return creds.valid
        except Exception as exc:  # noqa: BLE001
            _logger.warning("token refresh failed: %s", exc)
            return False
    return False


def save_token_file(creds: Credentials, path: str) -> None:
    """Save credentials to a token.json file with 0600 permissions."""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    _logger.info("saved OAuth token to %s (mode 0600)", target)


def check_token_permissions(path: str) -> bool:
    """Return True if the token file is readable only by its owner (0600)."""
    mode = os.stat(path).st_mode & 0o777
    return (mode & 0o077) == 0


class OAuthSession:
    """Encapsulates an InstalledAppFlow to preserve PKCE code_verifier.

    Both the initial authorization URL generation and the subsequent token
    exchange must use the exact same Flow instance so the PKCE code_verifier
    matches Google's expected challenge.
    """

    def __init__(
        self,
        client_secret_path: str,
        token_path: str | None = None,
        redirect_uri: str = "http://localhost:8080",
    ):
        from google_auth_oauthlib.flow import InstalledAppFlow

        self.client_secret_path = os.path.abspath(client_secret_path)
        self.token_path = os.path.abspath(token_path) if token_path else None
        # Ensure redirect_uri has no trailing slash (e.g. http://localhost:8080)
        self.redirect_uri = redirect_uri.rstrip("/")
        self.flow = InstalledAppFlow.from_client_secrets_file(self.client_secret_path, SCOPES)
        self.flow.redirect_uri = self.redirect_uri
        self._auth_url: str | None = None
        self.credentials: Credentials | None = None

    @property
    def auth_url(self) -> str:
        """Generate and cache the OAuth authorization URL."""
        if self._auth_url is None:
            self._auth_url, _ = self.flow.authorization_url(
                access_type="offline",
                prompt="consent",
                include_granted_scopes="true",
            )
        return self._auth_url

    def exchange(self, code_or_url: str) -> Credentials:
        """Exchange authorization code or full redirect URL for credentials.

        Accepts either:
        - raw authorization code: "4/0AfgeX..."
        - full redirect URL: "http://localhost:8080/?state=...&code=4%2F0Af...&scope=..."
        """
        from urllib.parse import parse_qs, urlparse

        input_str = code_or_url.strip().strip("'\"")
        code = input_str
        if "?" in input_str or "code=" in input_str:
            query_str = urlparse(input_str).query or input_str
            query = parse_qs(query_str)
            if "code" not in query:
                raise ValueError("URL 中未找到 'code' 参数，请确认复制了完整的浏览器地址栏 URL")
            code = query["code"][0]

        if not code:
            raise ValueError("授权码为空")

        _logger.debug("exchanging authorization code for token (redirect_uri=%s)", self.redirect_uri)
        self.flow.fetch_token(code=code)
        self.credentials = self.flow.credentials

        if self.token_path and self.credentials:
            save_token_file(self.credentials, self.token_path)

        return self.credentials


def headless_oauth_flow(
    client_secret_path: str,
    token_path: str | None = None,
    redirect_uri: str = "http://localhost:8080",
    input_fn=input,
    print_fn=print,
) -> Credentials:
    """OAuth login for headless servers (no local browser needed).

    Prints an authorization URL, asks the user to paste the redirect URL the
    browser lands on, then exchanges the code. A single flow object is kept
    across both steps so the PKCE ``code_verifier`` matches.
    """
    session = OAuthSession(client_secret_path, token_path=token_path, redirect_uri=redirect_uri)
    _logger.info("starting headless OAuth flow (redirect %s)", session.redirect_uri)
    print_fn("在浏览器中打开下面的 URL 并完成授权：")
    print_fn(session.auth_url)
    redirect_url = input_fn(
        "授权后，把浏览器跳转到的 URL（形如 http://localhost:8080/?state=...&code=...）粘贴到这里: "
    ).strip()
    return session.exchange(redirect_url)


def ensure_credentials(
    client_secret_path: str,
    token_path: str | None = None,
    interactive: bool | None = None,
    web_port: int | None = 8080,
    wait_for_web: bool = True,
) -> Credentials:
    """Load valid credentials, refreshing or prompting OAuth login as needed.

    Order of precedence:
    1. TUBETAPE_TOKEN environment variable (if set and valid).
    2. token.json file (if exists, valid or refreshable; refreshed token is saved back).
    3. Web OAuth flow (if web server is running and wait_for_web is True).
    4. Headless terminal OAuth flow (if interactive/TTY).
    5. If non-interactive and no web server, raises RuntimeError explaining how to authorize.
    """
    token_str = os.environ.get(TOKEN_ENV)
    if token_str:
        try:
            creds = credentials_from_token_string(token_str)
            if is_credentials_valid(creds):
                return creds
        except Exception as exc:  # noqa: BLE001
            _logger.warning("TUBETAPE_TOKEN environment variable invalid: %s", exc)

    if token_path and os.path.exists(token_path):
        try:
            creds = load_token_file(token_path)
            if is_credentials_valid(creds):
                # If refreshed, persist back
                try:
                    save_token_file(creds, token_path)
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("could not save refreshed token to %s: %s", token_path, exc)
                return creds
            _logger.warning("token file %s is expired or invalid and cannot be refreshed", token_path)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("could not load token from %s: %s", token_path, exc)

    client_secret_target = os.path.abspath(client_secret_path)
    if not os.path.exists(client_secret_target):
        _logger.error("client_secret.json not found at %s", client_secret_target)
        raise FileNotFoundError(
            f"OAuth client_secret.json not found at '{client_secret_target}'.\n"
            "Please download client_secret.json from Google Cloud Console and place it there."
        )

    # Check if Web OAuth flow is available (WebServer is active)
    from . import web

    if wait_for_web and web.is_running():
        actual_port = getattr(web, "get_web_port", lambda: web_port or 8080)()
        redirect_uri = f"http://localhost:{actual_port}"
        session = OAuthSession(client_secret_target, token_path=token_path, redirect_uri=redirect_uri)
        web.set_web_oauth_session(session)
        auth_url = session.auth_url

        _logger.warning("=" * 66)
        _logger.warning("TubeTape 尚未获得 YouTube 授权。")
        _logger.warning("请在浏览器中打开 Web 控制台完成一键授权：")
        _logger.warning("  http://localhost:%d  (或 http://<服务器IP>:%d)", actual_port, actual_port)
        _logger.warning("或直接在浏览器中打开以下 Google 授权链接：")
        _logger.warning("  %s", auth_url)
        _logger.warning("=" * 66)

        try:
            creds = web.wait_for_auth(timeout=3600.0)
            _logger.info("YouTube OAuth 授权成功，凭据已生效")
            return creds
        except Exception as exc:
            _logger.error("等待 Web 授权失败或超时: %s", exc)
            raise

    if interactive is None:
        interactive = sys.stdin.isatty()

    if not interactive:
        msg = (
            f"token.json 不存在或已失效，且当前处于非交互式环境。\n"
            f"必须先通过交互模式完成 Google OAuth 授权：\n\n"
            f"  docker compose run --rm tubetape --login\n\n"
            f"或在终端中运行：\n"
            f"  tubetape --client-secret \"{client_secret_target}\" --login\n"
        )
        _logger.error(msg)
        raise RuntimeError(msg)

    _logger.info("launching OAuth login flow with %s ...", client_secret_target)
    return headless_oauth_flow(client_secret_target, token_path=token_path)
