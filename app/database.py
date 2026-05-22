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
        minPoolSize=10,
        maxPoolSize=300,
        maxIdleTimeMS=45000,
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
    try:
        # ── Users ─────────────────────────────────────────────────────────────
        await _db.users.create_index(
            [("email", ASCENDING)], unique=True, background=True
        )
        await _db.users.create_index(
            [("username", ASCENDING)], unique=True, background=True
        )
        await _db.users.create_index(
            [("google_id", ASCENDING)], sparse=True, background=True
        )

        # ── Email Verifications — TTL: auto-delete after 10 min ───────────────
        # MongoDB deletes the doc when expires_at is reached (expireAfterSeconds=0).
        # This is a hard cleanup; the service layer also checks expiry manually.
        await _db.email_verifications.create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,
            background=True,
            name="email_verifications_ttl",
        )
        await _db.email_verifications.create_index(
            [("email", ASCENDING)],
            unique=True,
            background=True,
            name="email_verifications_email",
        )

        # ── Posts ─────────────────────────────────────────────────────────────
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

        # ── Conversations ─────────────────────────────────────────────────────
        await _db.conversations.create_index(
            [("participants", ASCENDING)],
            background=True,
            name="conversations_participants",
        )
        await _db.conversations.create_index(
            [("participants", ASCENDING), ("last_message_time", DESCENDING)],
            background=True,
            name="conversations_participants_last_msg",
        )

        # ── Messages ──────────────────────────────────────────────────────────
        await _db.messages.create_index(
            [("conversation_id", ASCENDING), ("created_at", DESCENDING)],
            background=True,
            name="messages_conversation",
        )

        # ── Notifications ─────────────────────────────────────────────────────
        await _db.notifications.create_index(
            [("recipient_id", ASCENDING), ("read", ASCENDING), ("created_at", DESCENDING)],
            background=True,
            name="notifications_recipient",
        )
        try:
            await _db.notifications.drop_index("notifications_ttl_30d")
            print("[DB] Removed notifications TTL index; notifications now persist until deleted.")
        except Exception:
            pass

        # ── Follows ───────────────────────────────────────────────────────────
        await _db.follows.create_index(
            [("follower_id", ASCENDING), ("following_id", ASCENDING)],
            unique=True,
            background=True,
            name="follows_pair",
        )
        await _db.follows.create_index(
            [("following_id", ASCENDING)], background=True, name="follows_following"
        )

        # ── Refresh tokens ────────────────────────────────────────────────────
        await _db.refresh_tokens.create_index(
            [("token", ASCENDING)], unique=True, background=True
        )
        await _db.refresh_tokens.create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,
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
