"""
Integration tests — real FastAPI routes, end-to-end, against an in-memory Mongo
(mongomock-motor) injected via dependency override. No real DB / network needed.

Covers the critical request → route → DB paths: auth, profile, follow, posts,
block. The rate limiter is disabled here so tests are deterministic.
"""
import datetime

import httpx
import pytest
from bson import ObjectId
from httpx import ASGITransport
from mongomock_motor import AsyncMongoMockClient

from app.main import app
from app.database import get_db
from app.limiter import limiter
from app.utils.jwt_handler import create_access_token
from app.utils.password import hash_password


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


@pytest.fixture
def tdb():
    """In-memory Mongo wired into the app via dependency override."""
    db = AsyncMongoMockClient()["trandia_test"]
    app.dependency_overrides[get_db] = lambda: db
    limiter.enabled = False
    yield db
    app.dependency_overrides.clear()
    limiter.enabled = True


def _client():
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(uid: str, email: str = "u@t.com"):
    return {"Authorization": f"Bearer {create_access_token(uid, email)}"}


async def _seed_user(db, username="testu", email="t@t.com", password=None):
    doc = {
        "_id": ObjectId(), "name": "Test", "username": username, "email": email,
        "is_google_user": False, "created_at": _now(),
        "followers_count": 0, "following_count": 0,
    }
    if password:
        doc["password_hash"] = await hash_password(password)
    await db.users.insert_one(doc)
    return doc


# ── Auth / profile ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_me_requires_auth(tdb):
    async with _client() as ac:
        r = await ac.get("/users/me")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_me_returns_profile(tdb):
    u = await _seed_user(tdb)
    async with _client() as ac:
        r = await ac.get("/users/me", headers=_auth(str(u["_id"])))
    assert r.status_code == 200
    assert r.json()["username"] == "testu"


@pytest.mark.asyncio
async def test_login_wrong_password_rejected(tdb):
    await _seed_user(tdb, email="login@t.com", password="correct-horse")
    async with _client() as ac:
        r = await ac.post("/auth/login", json={"email": "login@t.com", "password": "WRONG"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_success_returns_tokens(tdb):
    await _seed_user(tdb, email="ok@t.com", password="correct-horse")
    async with _client() as ac:
        r = await ac.post("/auth/login", json={"email": "ok@t.com", "password": "correct-horse"})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["refresh_token"]


# ── Follow / block ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_follow_creates_relationship(tdb):
    me = await _seed_user(tdb, username="me", email="me@t.com")
    target = await _seed_user(tdb, username="target", email="target@t.com")
    async with _client() as ac:
        r = await ac.post(f"/users/{target['_id']}/follow", headers=_auth(str(me["_id"])))
    assert r.status_code == 200 and r.json()["following"] is True
    rel = await tdb.follows.find_one(
        {"follower_id": str(me["_id"]), "following_id": str(target["_id"])}
    )
    assert rel is not None


@pytest.mark.asyncio
async def test_cannot_follow_self(tdb):
    me = await _seed_user(tdb, username="solo", email="solo@t.com")
    async with _client() as ac:
        r = await ac.post(f"/users/{me['_id']}/follow", headers=_auth(str(me["_id"])))
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_block_creates_record(tdb):
    me = await _seed_user(tdb, username="blocker", email="b@t.com")
    target = await _seed_user(tdb, username="blocked", email="bd@t.com")
    async with _client() as ac:
        r = await ac.post(f"/users/{target['_id']}/block", headers=_auth(str(me["_id"])))
    assert r.status_code == 200
    blk = await tdb.blocks.find_one(
        {"blocker_id": str(me["_id"]), "blocked_id": str(target["_id"])}
    )
    assert blk is not None


# ── Posts ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_and_list_post(tdb):
    u = await _seed_user(tdb, username="poster", email="post@t.com")
    payload = {
        "media_url": "https://res.cloudinary.com/demo/image/upload/v1/trandia/posts/x.jpg",
        "media_type": "image",
        "caption": "hello world",
    }
    async with _client() as ac:
        created = await ac.post("/posts/", json=payload, headers=_auth(str(u["_id"])))
        assert created.status_code == 200
        assert created.json()["caption"] == "hello world"
        feed = await ac.get("/posts/", headers=_auth(str(u["_id"])))
    assert feed.status_code == 200
    assert len(feed.json()["posts"]) >= 1


@pytest.mark.asyncio
async def test_create_post_rejects_non_cloudinary_url(tdb):
    u = await _seed_user(tdb, username="poster2", email="post2@t.com")
    payload = {"media_url": "https://evil.example.com/x.jpg", "media_type": "image"}
    async with _client() as ac:
        r = await ac.post("/posts/", json=payload, headers=_auth(str(u["_id"])))
    assert r.status_code == 400
