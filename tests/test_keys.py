"""Tests for vl.lib.keys — Ed25519 keygen, file layout, assertion signing."""

from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from vl.lib import keys


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def test_generate_keypair_layout_and_perms(tmp_path: Path) -> None:
    priv, pub = keys.generate_keypair(tmp_path / "sa-1")

    assert priv.read_bytes().__len__() == 64  # 32 seed + 32 pubkey
    assert pub.read_bytes().__len__() == 32
    assert priv.read_bytes()[32:] == pub.read_bytes()  # tail is the public key
    assert stat.S_IMODE(priv.stat().st_mode) == 0o600
    assert stat.S_IMODE(pub.stat().st_mode) == 0o644


def test_public_key_b64url_roundtrips(tmp_path: Path) -> None:
    _, pub = keys.generate_keypair(tmp_path / "sa-1")
    encoded = keys.public_key_b64url(pub)
    assert "=" not in encoded
    assert _b64url_decode(encoded) == pub.read_bytes()


def test_install_keypair_replaces(tmp_path: Path) -> None:
    dest = tmp_path / "keys" / "sa-1"
    keys.generate_keypair(dest)
    old_priv = (dest / "private.key").read_bytes()

    staging = tmp_path / "staging"
    new_priv, new_pub = keys.generate_keypair(staging)
    fp, _ = keys.install_keypair(new_priv, new_pub, dest)

    assert fp == dest / "private.key"
    assert (dest / "private.key").read_bytes() != old_priv
    assert not new_priv.exists()  # moved, not copied


def test_sign_assertion_structure_and_signature(tmp_path: Path) -> None:
    priv, pub = keys.generate_keypair(tmp_path / "sa-1")
    token = keys.sign_assertion(
        priv, subject="sa-1", audience="http://idp/auth/service-account/authenticate"
    )

    header_b64, payload_b64, sig_b64 = token.split(".")
    header = json.loads(_b64url_decode(header_b64))
    payload = json.loads(_b64url_decode(payload_b64))

    assert header == {"alg": "EdDSA", "typ": "JWT"}
    assert payload["iss"] == payload["sub"] == "sa-1"
    assert payload["aud"] == "http://idp/auth/service-account/authenticate"
    assert payload["exp"] - payload["iat"] == keys.ASSERTION_TTL_SECONDS
    assert "jti" in payload

    public_key = Ed25519PublicKey.from_public_bytes(pub.read_bytes())
    public_key.verify(_b64url_decode(sig_b64), f"{header_b64}.{payload_b64}".encode())


def test_sign_assertion_rejects_bad_key_size(tmp_path: Path) -> None:
    bad = tmp_path / "bad.key"
    bad.write_bytes(b"too short")
    with pytest.raises(ValueError):
        keys.sign_assertion(bad, subject="x", audience="y")
