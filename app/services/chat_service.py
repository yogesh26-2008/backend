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
        # Maps user_id string to a list of active WebSockets
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: str):
        if user_id in self.active_connections:
            self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def send_personal_message(self, message: str, user_id: str):
        if user_id in self.active_connections:
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_text(message)
                except Exception:
                    pass

manager = ConnectionManager()

async def get_or_create_conversation(current_user_id: str, participant_username: str, db):
    # Find participant
    participant = await db.users.find_one({"username": participant_username.lower()})
    if not participant:
        raise ValueError("User not found")
    
    participant_id = str(participant["_id"])
    if current_user_id == participant_id:
        raise ValueError("Cannot start a conversation with yourself")

    participants = [current_user_id, participant_id]
    
    # Check if a 1-on-1 conversation already exists between these two
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
        "unread_counts": {current_user_id: 0, participant_id: 0},
        "created_at": datetime.now(timezone.utc)
    }
    result = await db.conversations.insert_one(new_conv)
    return str(result.inserted_id)

async def get_user_conversations(user_id: str, db) -> List[ConversationResponse]:
    cursor = db.conversations.find({"participants": user_id}).sort("last_message_time", -1)
    convs = await cursor.to_list(length=100)
    
    response = []
    for c in convs:
        # Fetch user details for participants
        participant_users = []
        for pid in c["participants"]:
            user_doc = await db.users.find_one({"_id": ObjectId(pid)})
            if user_doc:
                participant_users.append(
                    UserResponse(
                        id=str(user_doc["_id"]),
                        name=user_doc["name"],
                        username=user_doc["username"],
                        email=user_doc["email"],
                        picture=user_doc.get("picture"),
                        is_google_user=user_doc.get("is_google_user", False),
                        created_at=user_doc["created_at"]
                    )
                )

        response.append(
            ConversationResponse(
                id=str(c["_id"]),
                participants=participant_users,
                last_message=c.get("last_message"),
                last_message_time=c.get("last_message_time"),
                unread_counts=c.get("unread_counts", {}),
                is_group=c.get("is_group", False),
                name=c.get("name")
            )
        )
    return response

async def get_conversation_messages(conversation_id: str, db, skip: int = 0, limit: int = 50) -> List[MessageResponse]:
    cursor = db.messages.find({"conversation_id": conversation_id}).sort("created_at", -1).skip(skip).limit(limit)
    messages = await cursor.to_list(length=limit)
    
    response = []
    # Reverse to return chronologically (oldest to newest) since frontend likely wants to append/prepend appropriately,
    # or keep it descending so frontend can use reverse ListView. We'll keep it descending (newest first)
    for m in messages:
        response.append(
            MessageResponse(
                id=str(m["_id"]),
                conversation_id=m["conversation_id"],
                sender_id=m["sender_id"],
                text=m["text"],
                created_at=m["created_at"],
                read_by=m.get("read_by", [])
            )
        )
    return response

async def save_message(conversation_id: str, sender_id: str, text: str, db):
    # Verify conversation exists and user is participant
    conv = await db.conversations.find_one({"_id": ObjectId(conversation_id), "participants": sender_id})
    if not conv:
        raise ValueError("Conversation not found or unauthorized")

    now = datetime.now(timezone.utc)
    new_message = {
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "text": text,
        "created_at": now,
        "read_by": [sender_id]
    }
    result = await db.messages.insert_one(new_message)
    msg_id = str(result.inserted_id)

    # Update conversation last_message and unread counts
    # Increment unread count for all participants except sender
    unread_updates = {}
    for pid in conv["participants"]:
        if pid != sender_id:
            current_unread = conv.get("unread_counts", {}).get(pid, 0)
            unread_updates[f"unread_counts.{pid}"] = current_unread + 1

    update_doc = {
        "$set": {
            "last_message": text,
            "last_message_time": now
        }
    }
    if unread_updates:
        update_doc["$set"].update(unread_updates) # type: ignore

    await db.conversations.update_one({"_id": ObjectId(conversation_id)}, update_doc)

    return MessageResponse(
        id=msg_id,
        conversation_id=conversation_id,
        sender_id=sender_id,
        text=text,
        created_at=now,
        read_by=[sender_id]
    ), conv["participants"]

async def mark_messages_read(conversation_id: str, user_id: str, db):
    # Update all messages in this conversation not read by user_id
    await db.messages.update_many(
        {"conversation_id": conversation_id, "read_by": {"$ne": user_id}},
        {"$addToSet": {"read_by": user_id}}
    )
    # Reset unread count in conversation
    await db.conversations.update_one(
        {"_id": ObjectId(conversation_id)},
        {"$set": {f"unread_counts.{user_id}": 0}}
    )
