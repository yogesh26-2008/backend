from typing import Dict, List
from fastapi import WebSocket
import json
from datetime import datetime, timezone
from bson import ObjectId
from app.models.chat import MessageResponse, ConversationResponse
from app.models.user import UserResponse
from app.database import get_db

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
    cursor = db.conversations.find({"participants": user_id}).sort("last_message_time", -1)
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
    return response


async def get_conversation_messages(conversation_id: str, db, skip: int = 0, limit: int = 50) -> List[MessageResponse]:
    cursor = db.messages.find(
        {"conversation_id": conversation_id}
    ).sort("created_at", -1).skip(skip).limit(limit)
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
                encrypted_aes_keys=m.get("encrypted_aes_keys", {})
            )
        )
    return response


async def save_message(conversation_id: str, sender_id: str, text: str, db, encrypted_aes_keys: dict = None):
    """Save a message and atomically increment unread counts."""
    # Verify conversation exists and user is participant
    conv = await db.conversations.find_one({
        "_id": ObjectId(conversation_id),
        "participants": sender_id
    })
    if not conv:
        raise ValueError("Conversation not found or unauthorized")

    now = datetime.now(timezone.utc)
    new_message = {
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "text": text,
        "created_at": now,
        "read_by": [sender_id],
        "encrypted_aes_keys": encrypted_aes_keys or {}
    }
    result = await db.messages.insert_one(new_message)
    msg_id = str(result.inserted_id)

    # BUG FIX: Previously used $set with read-then-write for unread_counts.
    # This caused a race condition: two concurrent messages would both read
    # the same count (e.g. 0) and set it to 1 instead of 2.
    # Fix: use MongoDB $inc which is atomic — it increments regardless of
    # the current value and initializes the field to 1 if it doesn't exist.
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

    return MessageResponse(
        id=msg_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        text=text,
        created_at=now,
        read_by=[sender_id],
        encrypted_aes_keys=encrypted_aes_keys or {}
    ), conv["participants"]


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
