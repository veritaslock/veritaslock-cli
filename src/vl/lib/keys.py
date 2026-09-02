"""Ed25519 key material for service accounts.

Keys are stored on disk in the same raw libsodium format the existing
``provision_service_account.sh`` / ``gen_assertion.py`` use:

* ``private.key`` — 64 bytes: 32-byte seed followed by the 32-byte public key.
* ``public.key``  — 32 bytes, raw.

`vl` only ever stores the *path* to these files in ``store.db`` (§2 of the spec),
never the bytes.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

ASSERTION_TTL_SECONDS = 300  # matches gen_assertion.py


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_keypair(dest_dir: Path) -> tuple[Path, Path]:
    """Write a fresh keypair into ``dest_dir``; return ``(private_path, public_path)``.

    Overwrites any existing files in place (used by both provisioning and rotation).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)

    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    public_raw = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )

    private_path = dest_dir / "private.key"
    public_path = dest_dir / "public.key"
    private_path.write_bytes(seed + public_raw)
    public_path.write_bytes(public_raw)
    os.chmod(private_path, 0o600)
    os.chmod(public_path, 0o644)
    return private_path, public_path


def import_private_key(src: Path, dest_dir: Path) -> tuple[Path, Path]:
    """Copy an existing raw Ed25519 private key into ``dest_dir`` in managed layout.

    Accepts a 32-byte seed or a 64-byte (seed ‖ public key) file, and writes the
    canonical pair: ``private.key`` (64 bytes) plus a ``public.key`` derived from
    it. The public key is always recomputed from the seed, so a caller only ever
    has to supply the private half. Returns ``(private_path, public_path)``.
    """
    raw = Path(src).read_bytes()
    if len(raw) not in (32, 64):
        raise ValueError(
            f"{src}: expected a 32- or 64-byte raw Ed25519 key, got {len(raw)} bytes"
        )
    private_key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    seed = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    private_path = dest_dir / "private.key"
    public_path = dest_dir / "public.key"
    private_path.write_bytes(seed + public_raw)
    public_path.write_bytes(public_raw)
    os.chmod(private_path, 0o600)
    os.chmod(public_path, 0o644)
    return private_path, public_path


def install_keypair(
    private_src: Path, public_src: Path, dest_dir: Path
) -> tuple[Path, Path]:
    """Move a staged keypair into its final home, replacing what's there.

    The staged-swap primitive for key rotation: generate the new keys to a temp
    dir, then move them in only after the server has accepted the new public key.
    Currently unused — `vl svc-acct rotate-keys` was removed pending server
    support (see docs/features/cli-implementation/svc-acct-key-rotation-gap.md) —
    but kept for when rotation returns.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    private_dest = dest_dir / "private.key"
    public_dest = dest_dir / "public.key"
    os.replace(private_src, private_dest)
    os.replace(public_src, public_dest)
    os.chmod(private_dest, 0o600)
    os.chmod(public_dest, 0o644)
    return private_dest, public_dest


def public_key_b64url(public_key_path: Path) -> str:
    """base64url (unpadded) encoding of the 32-byte raw public key, for the API."""
    return _b64url(Path(public_key_path).read_bytes())


def _load_private_key(private_key_path: Path) -> Ed25519PrivateKey:
    raw = Path(private_key_path).read_bytes()
    if len(raw) not in (32, 64):
        raise ValueError(
            f"{private_key_path}: expected a 32- or 64-byte raw Ed25519 key, "
            f"got {len(raw)} bytes"
        )
    return Ed25519PrivateKey.from_private_bytes(raw[:32])


def sign_assertion(
    private_key_path: Path,
    *,
    subject: str,
    audience: str,
    ttl_seconds: int = ASSERTION_TTL_SECONDS,
) -> str:
    """Build and sign a short-lived EdDSA JWT assertion (equivalent to gen_assertion.py)."""
    private_key = _load_private_key(private_key_path)

    header = {"alg": "EdDSA", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": subject,
        "sub": subject,
        "aud": audience,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + ttl_seconds,
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    signature = private_key.sign(signing_input.encode())
    return f"{signing_input}.{_b64url(signature)}"
