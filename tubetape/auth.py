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
    from urllib.parse import parse_qs, urlparse

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
    flow.redirect_uri = redirect_uri
    _logger.info("starting headless OAuth flow (redirect %s)", redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    print_fn("在浏览器中打开下面的 URL 并完成授权：")
    print_fn(auth_url)
    redirect_url = input_fn(
        "授权后，把浏览器跳转到的 URL（形如 http://localhost:8080/?state=...&code=...）粘贴到这里: "
    ).strip()

    query = parse_qs(urlparse(redirect_url).query)
    if "code" not in query:
        _logger.error("no 'code' parameter in the redirect URL")
        raise ValueError("no 'code' parameter in the redirect URL")
    code = query["code"][0]

    # Exchange exactly once, with the SAME flow: this keeps both the
    # redirect_uri (no trailing slash) and the PKCE code_verifier matching the
    # authorization request. Retrying with a different redirect_uri can
    # consume the one-time code, so don't.
    _logger.debug("exchanging authorization code for token")
    flow.fetch_token(code=code)
    credentials = flow.credentials

    if token_path is not None:
        save_token_file(credentials, token_path)

    return credentials


def ensure_credentials(
    client_secret_path: str,
    token_path: str | None = None,
    interactive: bool | None = None,
) -> Credentials:
    """Load valid credentials, refreshing or prompting OAuth login as needed.

    Order of precedence:
    1. TUBETAPE_TOKEN environment variable (if set and valid).
    2. token.json file (if exists, valid or refreshable; refreshed token is saved back).
    3. Interactive OAuth login using client_secret_path (if interactive/TTY).
    4. If non-interactive, raises RuntimeError explaining how to run interactive login.
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
