"""Token acquisition for authenticated `vl` commands (vl-identity-user-spec.md §7).

A command resolves its calling identity, then calls :func:`authed_call` with a
closure that does the actual HTTP. Tokens are read from ``token_cache``; on a miss
(or a mid-command ``401``) `vl` re-authenticates — silently for a tier-1 identity
whose password is stored, or via an interactive prompt for a tier-2 one (failing
fast with a clear message when there's no TTY to prompt on).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar

import typer

from vl.lib import api, store

T = TypeVar("T")


class AuthError(Exception):
    """Could not obtain a token for the resolved identity."""


def _persist(identity: store.Identity, result: api.TokenResult) -> str:
    issued = datetime.now(timezone.utc)
    expires = issued + timedelta(seconds=result.expires_in or 0)
    store.set_cached_token(identity.id, result.access_token, issued, expires)
    return result.access_token


def _reauthenticate(
    identity: store.Identity, idp_base_url: str, *, allow_prompt: bool | None
) -> str:
    if identity.kind == "SERVICE_ACCOUNT":
        sa = store.get_svc_acct(identity.id)
        if sa is None:
            raise AuthError(
                f"identity '{identity.label}' has no stored client secret — "
                f"re-import it with `vl identity import --kind SERVICE_ACCOUNT`"
            )
        # No tier-2 case for this kind (spec §1): the secret is always stored,
        # so this is unconditionally silent.
        return _persist(
            identity,
            api.service_account_token(
                idp_base_url, identity.principal_name, sa.client_secret_plaintext
            ),
        )

    credential = store.get_user_acct(identity.id)
    if credential is not None and credential.password_plaintext is not None:
        password = credential.password_plaintext  # tier 1 — silent
    else:
        if allow_prompt is None:
            allow_prompt = sys.stdin.isatty()
        if not allow_prompt:
            raise AuthError(
                f"identity '{identity.label}'s cached token has expired and no "
                f"password is stored — run `vl identity login "
                f"{identity.principal_name}` interactively"
            )
        password = typer.prompt(
            f"Password for identity '{identity.label}'", hide_input=True
        )
    return _persist(
        identity, api.login(idp_base_url, identity.principal_name, password)
    )


def get_token(
    identity: store.Identity,
    idp_base_url: str,
    *,
    allow_prompt: bool | None = None,
) -> str:
    """A usable token for ``identity`` — cached if valid, otherwise freshly issued."""
    cached = store.get_cached_token(identity.id)
    if cached is not None:
        return cached.token
    return _reauthenticate(identity, idp_base_url, allow_prompt=allow_prompt)


def authed_call(
    identity: store.Identity,
    idp_base_url: str,
    call: Callable[[api.IdpClient], T],
    *,
    allow_prompt: bool | None = None,
) -> T:
    """Run ``call(client)`` with a bearer token; on a ``401``, re-auth once and retry.

    ``call`` may issue several requests — on a retry it runs again in full, so keep
    it free of side effects other than the HTTP it performs.
    """
    token = get_token(identity, idp_base_url, allow_prompt=allow_prompt)
    try:
        with api.IdpClient(idp_base_url, token=token) as client:
            return call(client)
    except api.ApiError as exc:
        if exc.status_code != 401:
            raise
        store.clear_cached_token(identity.id)
        token = _reauthenticate(identity, idp_base_url, allow_prompt=allow_prompt)
        with api.IdpClient(idp_base_url, token=token) as client:
            return call(client)
