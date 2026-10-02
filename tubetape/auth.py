"""YouTube OAuth credential handling.

Credentials come from ``client_secret.json`` + ``token.json`` (or a token
string passed via environment). Scopes: ``youtube.upload`` and
``youtube.readonly``.
"""

from __future__ import annotations

import json
import os

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
        target = os.path.abspath(token_path)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
        os.chmod(target, 0o600)
        _logger.info("saved OAuth token to %s (mode 0600)", target)

    return credentials
