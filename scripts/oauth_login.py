#!/usr/bin/env python3
"""OAuth login that works on a headless server (no local browser required).

Prints an authorization URL for you to open in a browser, then exchanges the
redirect URL you paste back for a token.json. A single OAuth flow is kept
across both steps, so the PKCE code_verifier matches.

Usage:
    python scripts/oauth_login.py --client-secret client_secret.json --output token.json
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tubetape import auth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-secret", required=True, help="path to client_secret.json")
    parser.add_argument("--output", default="token.json", help="where to write token.json")
    args = parser.parse_args(argv)

    if not os.path.exists(args.client_secret):
        print(f"client secret not found: {args.client_secret}", file=sys.stderr)
        return 2

    try:
        auth.headless_oauth_flow(args.client_secret, token_path=args.output)
    except Exception as exc:  # noqa: BLE001 - report a clear error
        print(f"登录失败: {exc}", file=sys.stderr)
        return 1

    out = os.path.abspath(args.output)
    print(f"token written to {out}")
    print(f"owner-only permissions: {auth.check_token_permissions(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
