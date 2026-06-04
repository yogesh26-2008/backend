"""
Deterministic unit tests for the security-critical helpers.
No DB, no network — these run green anywhere and guard auth/password/JWT logic
against regressions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from app.config import settings
from app.utils.password import hash_password, verify_password
from app.utils.jwt_handler import (
    create_access_token,
    decode_token,
    create_refresh_token,
    hash_refresh_token,
)


# ── Password hashing ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_password_hash_and_verify():
    h = await hash_password("hunter2")
    assert h != "hunter2"                         # never stored in plaintext
    assert await verify_password("hunter2", h) is True
    assert await verify_password("wrong", h) is False


@pytest.mark.asyncio
async def test_password_uses_unique_salts():
    h1 = await hash_password("same-password")
    h2 = await hash_password("same-password")
    assert h1 != h2                               # salted → different hashes
    assert await verify_password("same-password", h1)
    assert await verify_password("same-password", h2)


# ── JWT access tokens ─────────────────────────────────────────────────────────

def test_jwt_roundtrip():
    tok = create_access_token("user123", "a@b.com")
    payload = decode_token(tok)
    assert payload is not None
    assert payload["sub"] == "user123"
    assert payload["email"] == "a@b.com"


def test_jwt_tampered_token_rejected():
    tok = create_access_token("user123", "a@b.com")
    assert decode_token(tok + "x") is None
    assert decode_token("garbage.token.value") is None
    assert decode_token("") is None


def test_jwt_expired_token_rejected():
    expired = jwt.encode(
        {"sub": "u", "email": "e", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    assert decode_token(expired) is None


def test_jwt_wrong_secret_rejected():
    forged = jwt.encode(
        {"sub": "u", "email": "e", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        "a-different-secret-key-entirely-1234",
        algorithm=settings.jwt_algorithm,
    )
    assert decode_token(forged) is None


# ── Refresh tokens ────────────────────────────────────────────────────────────

def test_refresh_token_unique_and_opaque():
    t1 = create_refresh_token()
    t2 = create_refresh_token()
    assert t1 != t2
    assert len(t1) >= 40                          # cryptographically long


def test_refresh_token_hash_is_stable_and_sha256():
    raw = create_refresh_token()
    h = hash_refresh_token(raw)
    assert h != raw                               # raw never stored
    assert hash_refresh_token(raw) == h           # deterministic for lookup
    assert len(h) == 64                           # sha256 hex digest
