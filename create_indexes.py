"""
create_indexes.py — Run this ONCE to set up MongoDB indexes for Trandia chat.

Usage:
  cd backend
  python create_indexes.py

These indexes are critical for performance at scale.
Without them, every chat fetch = full collection scan.
"""

import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGODB_URL") or os.getenv("MONGO_URL") or os.getenv("DATABASE_URL")
DB_NAME   = os.getenv("DB_NAME", "trandia")

async def create_indexes():
    if not MONGO_URL:
        print("❌ No MONGO_URL found in .env — aborting")
        return

    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    print(f"✅ Connected to MongoDB — database: {DB_NAME}")

    # ── conversations ──────────────────────────────────────────
    # Find conversations for a user, sorted by last message (chat list)
    await db.conversations.create_index(
        [("participants", 1), ("last_message_time", -1)],
        name="conv_by_participant_time"
    )
    print("✅ conversations: (participants, last_message_time DESC)")

    # Find 1-on-1 conversation between two users (get_or_create)
    await db.conversations.create_index(
        [("participants", 1), ("is_group", 1)],
        name="conv_participants_group"
    )
    print("✅ conversations: (participants, is_group)")

    # ── messages ──────────────────────────────────────────────
    # Fetch messages in a conversation, newest first (chat screen)
    await db.messages.create_index(
        [("conversation_id", 1), ("created_at", -1)],
        name="msg_by_conv_time"
    )
    print("✅ messages: (conversation_id, created_at DESC)")

    # Mark messages as read (find unread messages in a conversation)
    await db.messages.create_index(
        [("conversation_id", 1), ("read_by", 1)],
        name="msg_read_status"
    )
    print("✅ messages: (conversation_id, read_by)")

    # ── users ─────────────────────────────────────────────────
    # Username lookup for startConversation + search
    await db.users.create_index(
        [("username", 1)],
        unique=True,
        name="user_username_unique"
    )
    print("✅ users: username (unique)")

    await db.users.create_index(
        [("email", 1)],
        unique=True,
        name="user_email_unique"
    )
    print("✅ users: email (unique)")

    # Text search index for user search
    await db.users.create_index(
        [("username", "text"), ("name", "text")],
        name="user_text_search"
    )
    print("✅ users: text index (username, name)")

    # ── posts ─────────────────────────────────────────────────
    # General feed query (all posts newest first)
    await db.posts.create_index(
        [("_id", -1)],
        name="posts_by_id_desc"
    )
    print("✅ posts: (_id DESC) — general feed")

    # Shots feed query: filter by media_type + section, newest first
    await db.posts.create_index(
        [("media_type", 1), ("section", 1), ("_id", -1)],
        name="posts_shots_feed"
    )
    print("✅ posts: (media_type, section, _id DESC) — shots feed")

    # User posts query (profile screen)
    await db.posts.create_index(
        [("user_id", 1), ("_id", -1)],
        name="posts_by_user"
    )
    print("✅ posts: (user_id, _id DESC) — user profile feed")

    # Post likes — check if user liked a post
    await db.post_likes.create_index(
        [("post_id", 1), ("user_id", 1)],
        unique=True,
        name="post_likes_unique"
    )
    print("✅ post_likes: (post_id, user_id) unique")

    await db.post_likes.create_index(
        [("user_id", 1), ("post_id", 1)],
        name="post_likes_by_user"
    )
    print("✅ post_likes: (user_id, post_id)")

    # ── refresh_tokens ────────────────────────────────────────
    # Fast lookup by token string (used on every /auth/refresh call)
    await db.refresh_tokens.create_index(
        [("token", 1)],
        unique=True,
        name="refresh_token_unique"
    )
    print("✅ refresh_tokens: token (unique)")

    # Query by user_id to revoke all tokens on password change / security event
    await db.refresh_tokens.create_index(
        [("user_id", 1)],
        name="refresh_token_by_user"
    )
    print("✅ refresh_tokens: user_id")

    # Auto-expire documents 30 days after expires_at
    # (extra safety net in case manual revocation is missed)
    await db.refresh_tokens.create_index(
        [("expires_at", 1)],
        expireAfterSeconds=30 * 24 * 3600,
        name="refresh_token_ttl"
    )
    print("✅ refresh_tokens: expires_at (TTL — auto-delete after 30d)")

    # ── comments ──────────────────────────────────────────────
    # Fetch top-level comments for a post (oldest-first pagination)
    await db.comments.create_index(
        [("post_id", 1), ("parent_id", 1), ("_id", 1)],
        name="comments_by_post_parent_id"
    )
    print("comments: (post_id, parent_id, _id ASC)")

    # comment_likes — prevent duplicate likes, fast lookup
    await db.comment_likes.create_index(
        [("comment_id", 1), ("user_id", 1)],
        unique=True,
        name="comment_likes_unique"
    )
    print("comment_likes: (comment_id, user_id) unique")

    await db.comment_likes.create_index(
        [("user_id", 1), ("comment_id", 1)],
        name="comment_likes_by_user"
    )
    print("comment_likes: (user_id, comment_id)")

    print("\n All indexes created successfully!")
    client.close()

if __name__ == "__main__":
    asyncio.run(create_indexes())
