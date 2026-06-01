from typing import Dict, List, Optional
from fastapi import WebSocket
import json
import logging
from datetime import datetime, timezone
from bson import ObjectId
from app.models.chat import MessageResponse, ConversationResponse
from app.models.user import UserResponse
from app.database import get_db

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        # Maps user_id string to a list of active WebSockets (multi-tab support)
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: str):
        if user_id in self.active_connections:
            try:
                self.active_connections[user_id].remove(websocket)
            except ValueError:
                pass
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    def is_online(self, user_id: str) -> bool:
        return bool(self.active_connections.get(user_id))

    def get_online_of(self, user_ids: List[str]) -> List[str]:
        return [uid for uid in user_ids if self.is_online(uid)]

    async def send_personal_message(self, message: str, user_id: str):
        if user_id in self.active_connections:
            dead = []
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_text(message)
                except Exception:
                    dead.append(connection)
            for d in dead:
                try:
                    self.active_connections[user_id].remove(d)
                except ValueError:
                    pass

manager = ConnectionManager()


async def broadcast_presence(user_id: str, online: bool, db) -> None:
    """
    On connect: notify all conversation partners that user is online,
                and send the connecting user a list of which partners are online.
    On disconnect: notify all conversation partners that user is offline.
    """
    try:
        cursor = db.conversations.find({"participants": user_id}, {"participants": 1})
        convs = await cursor.to_list(length=200)

        partner_ids: set = set()
        for c in convs:
            for pid in c["participants"]:
                if pid != user_id:
                    partner_ids.add(pid)

        if not partner_ids:
            return

        presence_msg = json.dumps({"type": "presence", "user_id": user_id, "online": online})
        for pid in partner_ids:
            await manager.send_personal_message(presence_msg, pid)

        # On connect, tell the user which of their partners are already online
        if online:
            online_partners = manager.get_online_of(list(partner_ids))
            if online_partners:
                init_msg = json.dumps({
                    "type": "presence_init",
                    "online_user_ids": online_partners,
                })
                await manager.send_personal_message(init_msg, user_id)
    except Exception as e:
        logger.error(f"[presence] broadcast_presence error for {user_id}: {e}")

async def get_or_create_conversation(current_user_id: str, participant_username: str, db):
    # Find participant by username (case-insensitive)
    participant = await db.users.find_one({"username": participant_username.lower()})
    if not participant:
        raise ValueError("User not found")

    participant_id = str(participant["_id"])
    if current_user_id == participant_id:
        raise ValueError("Cannot start a conversation with yourself")

    participants = [current_user_id, participant_id]

    # Check if a 1-on-1 conversation already exists
    existing_conv = await db.conversations.find_one({
        "is_group": False,
        "participants": {"$all": participants, "$size": 2}
    })

    if existing_conv:
        return str(existing_conv["_id"])

    # Create new conversation
    new_conv = {
        "participants": participants,
        "is_group": False,
        "name": None,
        "last_message": None,
        "last_message_time": None,
        # Initialize unread counts to 0 for all participants
        "unread_counts": {current_user_id: 0, participant_id: 0},
        "created_at": datetime.now(timezone.utc)
    }
    result = await db.conversations.insert_one(new_conv)
    return str(result.inserted_id)


async def get_user_conversations(user_id: str, db) -> List[ConversationResponse]:
    from app.cache import get_cache, set_cache
    cache_key = f"convs:{user_id}"
    cached = await get_cache(cache_key)
    if cached and isinstance(cached, list):
        try:
            return [ConversationResponse(**c) for c in cached]
        except Exception:
            pass  # stale cache schema — fall through to DB

    cursor = db.conversations.find(
        {"participants": user_id, "hidden_for": {"$ne": user_id}}
    ).sort("last_message_time", -1)
    convs = await cursor.to_list(length=100)

    # Gather all unique participant IDs across conversations
    all_pids = set()
    for c in convs:
        for pid in c["participants"]:
            all_pids.add(pid)

    # Query all participant users in a single bulk DB call
    user_docs = {}
    if all_pids:
        object_ids = []
        for pid in all_pids:
            try:
                object_ids.append(ObjectId(pid))
            except Exception:
                pass
        cursor_users = db.users.find({"_id": {"$in": object_ids}})
        async for u in cursor_users:
            user_docs[str(u["_id"])] = u

    response = []
    for c in convs:
        participant_users = []
        for pid in c["participants"]:
            user_doc = user_docs.get(pid)
            if user_doc:
                participant_users.append(
                    UserResponse(
                        id=str(user_doc["_id"]),
                        name=user_doc.get("name", ""),
                        username=user_doc.get("username", ""),
                        email=user_doc.get("email", ""),
                        picture=user_doc.get("picture"),
                        is_google_user=user_doc.get("is_google_user", False),
                        created_at=user_doc["created_at"],
                        public_key=user_doc.get("public_key")
                    )
                )

        if not participant_users:
            continue  # skip broken conversations

        response.append(
            ConversationResponse(
                id=str(c["_id"]),
                participants=participant_users,
                last_message=c.get("last_message"),
                last_message_time=c.get("last_message_time"),
                last_message_encrypted_aes_keys=c.get("last_message_encrypted_aes_keys", {}),
                unread_counts=c.get("unread_counts", {}),
                is_group=c.get("is_group", False),
                name=c.get("name")
            )
        )

    try:
        await set_cache(cache_key, [r.model_dump(mode="json") for r in response], expire_seconds=60)
    except Exception:
        pass
    return response


async def get_conversation_messages(
    conversation_id: str,
    db,
    skip: int = 0,
    limit: int = 50,
    before_id: Optional[str] = None,
) -> List[MessageResponse]:
    from app.cache import get_cache, set_cache

    # Cache only the first page (no skip, no cursor)
    cache_key = f"msgs:{conversation_id}:{limit}" if (skip == 0 and not before_id) else None
    if cache_key:
        cached = await get_cache(cache_key)
        if cached and isinstance(cached, list):
            try:
                return [MessageResponse(**m) for m in cached]
            except Exception:
                pass

    query: dict = {"conversation_id": conversation_id}
    if before_id:
        try:
            before_oid = ObjectId(before_id)
            query["_id"] = {"$lt": before_oid}
        except Exception:
            pass

    cursor = db.messages.find(query).sort("created_at", -1)
    if not before_id:
        cursor = cursor.skip(skip)
    cursor = cursor.limit(limit)
    messages = await cursor.to_list(length=limit)

    response = []
    for m in messages:
        response.append(
            MessageResponse(
                id=str(m["_id"]),
                conversation_id=m["conversation_id"],
                sender_id=m["sender_id"],
                text=m["text"],
                created_at=m["created_at"],
                read_by=m.get("read_by", []),
                encrypted_aes_keys=m.get("encrypted_aes_keys", {}),
                reactions=m.get("reactions", {}),
                reply_to_id=m.get("reply_to_id"),
                reply_to_text=m.get("reply_to_text"),
            )
        )

    if cache_key:
        try:
            await set_cache(cache_key, [m.model_dump(mode="json") for m in response], expire_seconds=30)
        except Exception:
            pass
    return response


async def save_message(
    conversation_id: str,
    sender_id: str,
    text: str,
    db,
    encrypted_aes_keys: dict = None,
    created_at: Optional[datetime] = None,
    reply_to_id: Optional[str] = None,
    reply_to_text: Optional[str] = None,
):
    """Save a message and atomically increment unread counts."""
    if len(text) > 10000:
        raise ValueError("Message too long (max 10000 characters)")

    # Verify conversation exists and user is participant
    conv = await db.conversations.find_one({
        "_id": ObjectId(conversation_id),
        "participants": sender_id
    })
    if not conv:
        raise ValueError("Conversation not found or unauthorized")

    # Block check — any participant may have blocked the sender
    other_ids = [pid for pid in conv["participants"] if pid != sender_id]
    for other_id in other_ids:
        block = await db.blocks.find_one({
            "$or": [
                {"blocker_id": other_id,  "blocked_id": sender_id},
                {"blocker_id": sender_id, "blocked_id": other_id},
            ]
        })
        if block:
            raise ValueError("blocked")

    now = created_at or datetime.now(timezone.utc)
    new_message = {
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "text": text,
        "created_at": now,
        "read_by": [sender_id],
        "encrypted_aes_keys": encrypted_aes_keys or {},
        "reactions": {},
        "reply_to_id": reply_to_id,
        "reply_to_text": reply_to_text,
    }
    result = await db.messages.insert_one(new_message)
    msg_id = str(result.inserted_id)

    inc_fields = {
        f"unread_counts.{pid}": 1
        for pid in conv["participants"]
        if pid != sender_id
    }

    update_doc: dict = {
        "$set": {
            "last_message": text,
            "last_message_time": now,
            "last_message_encrypted_aes_keys": encrypted_aes_keys or {},
        }
    }
    if inc_fields:
        update_doc["$inc"] = inc_fields

    await db.conversations.update_one(
        {"_id": ObjectId(conversation_id)},
        update_doc
    )

    # Invalidate Redis caches for this conversation and all participants
    try:
        from app.cache import delete_cache
        for pid in conv["participants"]:
            await delete_cache(f"convs:{pid}")
        for lim in (20, 30, 50, 100):
            await delete_cache(f"msgs:{conversation_id}:{lim}")
    except Exception:
        pass

    return MessageResponse(
        id=msg_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        text=text,
        created_at=now,
        read_by=[sender_id],
        encrypted_aes_keys=encrypted_aes_keys or {},
        reactions={},
        reply_to_id=reply_to_id,
        reply_to_text=reply_to_text,
    ), conv["participants"]


async def toggle_reaction(message_id: str, user_id: str, emoji: str, db):
    """Toggle a reaction on a message. Returns updated reactions dict."""
    try:
        msg_oid = ObjectId(message_id)
    except Exception:
        raise ValueError("Invalid message id")

    msg = await db.messages.find_one({"_id": msg_oid})
    if not msg:
        raise ValueError("Message not found")

    reactions: dict = msg.get("reactions", {})
    users_for_emoji: list = reactions.get(emoji, [])

    if user_id in users_for_emoji:
        # Remove reaction
        await db.messages.update_one(
            {"_id": msg_oid},
            {"$pull": {f"reactions.{emoji}": user_id}}
        )
        users_for_emoji.remove(user_id)
    else:
        # Add reaction
        await db.messages.update_one(
            {"_id": msg_oid},
            {"$addToSet": {f"reactions.{emoji}": user_id}}
        )
        users_for_emoji.append(user_id)

    # Cleanup empty emoji lists
    if not users_for_emoji and emoji in reactions:
        await db.messages.update_one(
            {"_id": msg_oid},
            {"$unset": {f"reactions.{emoji}": ""}}
        )
        reactions.pop(emoji, None)
    else:
        reactions[emoji] = users_for_emoji

    return reactions, msg["conversation_id"]


async def mark_messages_read(conversation_id: str, user_id: str, db):
    """Mark all unread messages in conversation as read and reset unread counter."""
    await db.messages.update_many(
        {"conversation_id": conversation_id, "read_by": {"$ne": user_id}},
        {"$addToSet": {"read_by": user_id}}
    )
    await db.conversations.update_one(
        {"_id": ObjectId(conversation_id)},
        {"$set": {f"unread_counts.{user_id}": 0}}
    )
    try:
        from app.cache import delete_cache
        await delete_cache(f"convs:{user_id}")
    except Exception:
        pass
