"""
API-key authentication for the migration routes.

One shared secret in `STACKMIGRATE_API_KEY`, sent as the `X-API-Key` header.
Deliberately simple: it stops "anyone who can reach the port can approve a PR",
which is the real risk (the approve route publishes code to a repository). It
is NOT per-user identity, rotation, or rate limiting — see the README's known
gaps before exposing this beyond a trusted network.

Fail-closed wiring: production startup (`app/main.py`) refuses to boot without a
key unless `STACKMIGRATE_ALLOW_NO_AUTH=1` is set explicitly, so forgetting the
secret can't silently deploy an open API. `create_app(api_key=None)` means
"no auth" and exists for tests and that explicit opt-out.
"""

from __future__ import annotations

import os
import secrets
from typing import Callable, Mapping

from fastapi import Header, HTTPException


def load_api_key(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    key = env.get("STACKMIGRATE_API_KEY")
    if key:
        return key
    if env.get("STACKMIGRATE_ALLOW_NO_AUTH") == "1":
        return None
    raise RuntimeError(
        "STACKMIGRATE_API_KEY is not set. Set it to a long random secret "
        "(e.g. `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`). "
        "For local development only, STACKMIGRATE_ALLOW_NO_AUTH=1 disables authentication."
    )


def make_api_key_dependency(api_key: str | None) -> Callable:
    async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if api_key is None:
            return
        if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), api_key.encode()):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    return require_api_key
