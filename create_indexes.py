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

    print("\n🎉 All indexes created successfully!")
    client.close()

if __name__ == "__main__":
    asyncio.run(create_indexes())
