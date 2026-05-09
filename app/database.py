import certifi
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from app.config import settings

_client: AsyncIOMotorClient = None
_db: AsyncIOMotorDatabase = None


async def connect_db():
    global _client, _db
    _client = AsyncIOMotorClient(
        settings.mongodb_url,
        serverSelectionTimeoutMS=30000,
        connectTimeoutMS=30000,
        socketTimeoutMS=30000,
        # BUG FIX: Added minPoolSize/maxPoolSize for concurrent user handling.
        # Default pool size (100) is fine but explicit is better documented.
        minPoolSize=5,
        maxPoolSize=100,
        tlsCAFile=certifi.where(),
    )
    _db = _client[settings.mongodb_db]
    try:
        await _client.admin.command("ping")
        print(f"[DB] ✅ Connected to MongoDB — database: {settings.mongodb_db}")
    except Exception as e:
        print(f"[DB] ❌ Could not connect — {e}")
        return
    await _create_indexes()


async def _create_indexes():
    """
    BUG FIX: Was only creating email + username indexes.
    Missing indexes caused full collection scans on every feed/notification
    query, which would be catastrophic at scale.
    All indexes are created with background=True so they don't block startup.
    """
    try:
        # ── Users ────────────────────────────────────────────────────────────
        await _db.users.create_index(
            [("email", ASCENDING)], unique=True, background=True
        )
        await _db.users.create_index(
            [("username", ASCENDING)], unique=True, background=True
        )
        await _db.users.create_index(
            [("google_id", ASCENDING)], sparse=True, background=True
        )

        # ── Posts (feed queries) ──────────────────────────────────────────────
        await _db.posts.create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            background=True,
            name="posts_user_feed",
        )
        await _db.posts.create_index(
            [("created_at", DESCENDING)], background=True, name="posts_global_feed"
        )

        # ── Stories — TTL: auto-delete after 24 h ────────────────────────────
        await _db.stories.create_index(
            [("created_at", ASCENDING)],
            expireAfterSeconds=86400,
            background=True,
            name="stories_ttl_24h",
        )
        await _db.stories.create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            background=True,
            name="stories_user_feed",
        )

        # ── Messages (DMs) ───────────────────────────────────────────────────
        await _db.messages.create_index(
            [("conversation_id", ASCENDING), ("created_at", DESCENDING)],
            background=True,
            name="messages_conversation",
        )

        # ── Notifications ────────────────────────────────────────────────────
        await _db.notifications.create_index(
            [("recipient_id", ASCENDING), ("read", ASCENDING), ("created_at", DESCENDING)],
            background=True,
            name="notifications_recipient",
        )
        # Auto-delete notifications after 30 days
        await _db.notifications.create_index(
            [("created_at", ASCENDING)],
            expireAfterSeconds=2592000,
            background=True,
            name="notifications_ttl_30d",
        )

        # ── Follows ──────────────────────────────────────────────────────────
        await _db.follows.create_index(
            [("follower_id", ASCENDING), ("following_id", ASCENDING)],
            unique=True,
            background=True,
            name="follows_pair",
        )
        await _db.follows.create_index(
            [("following_id", ASCENDING)], background=True, name="follows_following"
        )

        # ── Refresh tokens ───────────────────────────────────────────────────
        await _db.refresh_tokens.create_index(
            [("token", ASCENDING)], unique=True, background=True
        )
        await _db.refresh_tokens.create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,  # MongoDB removes doc when expires_at is reached
            background=True,
            name="refresh_tokens_ttl",
        )

        print("[DB] ✅ All indexes verified.")
    except Exception as e:
        print(f"[DB] ⚠️  Index creation warning: {e}")


async def close_db():
    global _client
    if _client:
        _client.close()
        print("[DB] MongoDB connection closed")


def get_db() -> AsyncIOMotorDatabase:
    return _db
