import logging

logger = logging.getLogger(__name__)
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
        maxPoolSize=100,   # 100 × 4 uvicorn workers = 400 conns — safe under Atlas shared-tier 500 cap
        maxIdleTimeMS=45000,
        tlsCAFile=certifi.where(),
    )
    _db = _client[settings.mongodb_db]
    try:
        await _client.admin.command("ping")
        logger.info(f"[DB] Connected to MongoDB — database: {settings.mongodb_db}")
    except Exception as e:
        logger.error(f"[DB] Could not connect: {e}")
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
        # Collaborator discovery — filter by account_type (creator/business/
        # professional). Sparse: only docs that have set an account_type are
        # indexed, keeping it small for the personal-account majority.
        await _db.users.create_index(
            [("account_type", ASCENDING)],
            sparse=True,
            background=True,
            name="users_account_type",
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

        # ── Stories ───────────────────────────────────────────────────────────
        # TTL index intentionally removed — cleanup job deletes Cloudinary assets
        # first, then MongoDB docs. If MongoDB TTL ran first, Cloudinary assets
        # would be orphaned forever. Drop any old TTL indexes.
        for _old_ttl in ("stories_ttl_24h", "stories_ttl_expires"):
            try:
                await _db.stories.drop_index(_old_ttl)
            except Exception:
                pass
        # Plain index on expires_at for efficient expiry queries (no auto-delete).
        await _db.stories.create_index(
            [("expires_at", ASCENDING)],
            background=True,
            name="stories_expiry",
        )
        await _db.stories.create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            background=True,
            name="stories_user_feed",
        )
        await _db.stories.create_index(
            [("hidden_from", ASCENDING)],
            background=True,
            name="stories_hidden_from",
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
        # Primary query: find({recipient_id}).sort(created_at, -1)
        # This index lets MongoDB skip the in-memory sort entirely.
        await _db.notifications.create_index(
            [("recipient_id", ASCENDING), ("created_at", DESCENDING)],
            background=True,
            name="notifications_by_time",
        )
        # Secondary index for unread-count queries (read filter + sort).
        await _db.notifications.create_index(
            [("recipient_id", ASCENDING), ("read", ASCENDING), ("created_at", DESCENDING)],
            background=True,
            name="notifications_recipient",
        )
        # Drop any old short (30-day) TTL, then add a generous 365-day TTL so the
        # collection can't grow unbounded — while keeping a full year of history
        # (no real user reads notifications older than that). Adjust or drop this
        # index if you want a different retention.
        try:
            await _db.notifications.drop_index("notifications_ttl_30d")
        except Exception:
            pass
        await _db.notifications.create_index(
            [("created_at", ASCENDING)],
            expireAfterSeconds=365 * 24 * 3600,
            background=True,
            name="notifications_ttl_365d",
        )

        # ── Comments ──────────────────────────────────────────────────────────
        # Primary: fetch top-level comments for a post (post_id + parent_id=null + _id sort)
        await _db.comments.create_index(
            [("post_id", ASCENDING), ("_id", ASCENDING)],
            background=True,
            name="comments_post_feed",
        )
        # Replies lookup: all replies for a set of parent_ids in one query
        await _db.comments.create_index(
            [("post_id", ASCENDING), ("parent_id", ASCENDING), ("_id", ASCENDING)],
            background=True,
            name="comments_replies",
        )
        # Author lookup: delete own comments, profile comment history
        await _db.comments.create_index(
            [("user_id", ASCENDING)],
            background=True,
            name="comments_user",
        )

        # ── Comment Likes ─────────────────────────────────────────────────────
        await _db.comment_likes.create_index(
            [("comment_id", ASCENDING), ("user_id", ASCENDING)],
            unique=True,
            background=True,
            name="comment_likes_pair",
        )
        await _db.comment_likes.create_index(
            [("user_id", ASCENDING), ("comment_id", ASCENDING)],
            background=True,
            name="comment_likes_user",
        )

        # ── Post Likes ────────────────────────────────────────────────────────
        await _db.post_likes.create_index(
            [("post_id", ASCENDING), ("user_id", ASCENDING)],
            unique=True,
            background=True,
            name="post_likes_post_user",
        )
        await _db.post_likes.create_index(
            [("user_id", ASCENDING), ("post_id", ASCENDING)],
            background=True,
            name="post_likes_user_post",
        )

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

        # ── Blocks ───────────────────────────────────────────────────────────
        await _db.blocks.create_index(
            [("blocker_id", ASCENDING), ("blocked_id", ASCENDING)],
            unique=True, background=True, name="blocks_pair",
        )
        await _db.blocks.create_index(
            [("blocker_id", ASCENDING)], background=True, name="blocks_by_blocker"
        )
        await _db.blocks.create_index(
            [("blocked_id", ASCENDING)], background=True, name="blocks_by_blocked"
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

        # ── Share links ───────────────────────────────────────────────────────
        await _db.share_links.create_index(
            [("token", ASCENDING)], unique=True, background=True, name="share_links_token"
        )
        await _db.share_links.create_index(
            [("videoId", ASCENDING)], background=True, name="share_links_video"
        )

        # ── Quiz ──────────────────────────────────────────────────────────────
        await _db.quizzes.create_index(
            [("quiz_id", ASCENDING)], unique=True, background=True, name="quizzes_quiz_id"
        )
        await _db.quizzes.create_index(
            [("user_id", ASCENDING)], background=True, name="quizzes_user_id"
        )

        # ── Transcript Cache ───────────────────────────────────────────────────
        await _db.transcript_cache.create_index(
            [("video_id", ASCENDING)], unique=True, background=True, name="transcript_cache_video_id"
        )

        logger.info("[DB] All indexes verified.")
    except Exception as e:
        logger.warning(f"[DB] Index creation warning: {e}")


async def close_db():
    global _client
    if _client:
        _client.close()
        logger.info("[DB] MongoDB connection closed")


def get_db() -> AsyncIOMotorDatabase:
    return _db
