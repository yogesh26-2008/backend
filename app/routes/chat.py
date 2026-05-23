from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query, HTTPException
from typing import List, Optional
import json
import logging
import asyncio
from datetime import datetime, timezone
from bson import ObjectId
from bson.errors import InvalidId

from app.database import get_db
from app.utils.jwt_handler import get_current_user_id, decode_token
from app.models.chat import ConversationResponse, MessageResponse, ConversationCreate
from app.services.chat_service import (
    manager,
    get_user_conversations,
    get_or_create_conversation,
    get_conversation_messages,
    save_message,
    mark_messages_read,
    toggle_reaction,
)
from app.services.notification_service import schedule_message_notification

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_client_created_at(value) -> Optional[datetime]:
    """
    NOTE: Hum client time ko IGNORE karte hain aur server UTC time use karte hain.
    Iska karan: client ka clock galat ho sakta hai ya timezone mismatch ho sakti hai
    jisse dono users ko alag-alag time dikh sakta tha.
    Server hamesha accurate UTC time use karta hai.
    """
    return None  # Always use server time (datetime.now(timezone.utc) in save_message)



@router.get("/conversations", response_model=List[ConversationResponse])
async def list_conversations(
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Get all conversations for the current user, sorted by last message."""
    return await get_user_conversations(user_id, db)


@router.post("/conversations")
async def start_conversation(
    data: ConversationCreate,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Get or create a 1-on-1 conversation."""
    try:
        conv_id = await get_or_create_conversation(user_id, data.participant_username, db)
        return {"conversation_id": conv_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{conversation_id}/messages", response_model=List[MessageResponse])
async def get_messages(
    conversation_id: str,
    skip: int = 0,
    limit: int = 50,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Get messages for a conversation (newest first)."""
    try:
        oid = ObjectId(conversation_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    conv = await db.conversations.find_one({"_id": oid, "participants": user_id})
    if not conv:
        raise HTTPException(status_code=403, detail="Not a participant in this conversation")

    asyncio.create_task(mark_messages_read(conversation_id, user_id, db))
    return await get_conversation_messages(conversation_id, db, skip, limit)


@router.post("/{conversation_id}/messages/{message_id}/react")
async def react_to_message(
    conversation_id: str,
    message_id: str,
    body: dict,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Toggle a reaction emoji on a message (REST fallback)."""
    emoji = body.get("emoji", "").strip()
    if not emoji:
        raise HTTPException(status_code=400, detail="emoji is required")
    if len(emoji) > 10:
        raise HTTPException(status_code=400, detail="Invalid emoji")

    try:
        reactions, conv_id = await toggle_reaction(message_id, user_id, emoji, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    conv = await db.conversations.find_one({"_id": ObjectId(conversation_id)})
    if conv:
        broadcast = json.dumps({
            "type": "react",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "reactions": reactions,
        })
        for pid in conv["participants"]:
            await manager.send_personal_message(broadcast, pid)

    return {"reactions": reactions}


@router.delete("/{conversation_id}/messages/{message_id}")
async def delete_message(
    conversation_id: str,
    message_id: str,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Delete a message (only sender can delete)."""
    try:
        msg_oid = ObjectId(message_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid message id")

    msg = await db.messages.find_one(
        {"_id": msg_oid, "conversation_id": conversation_id}
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg["sender_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this message")

    await db.messages.delete_one({"_id": msg_oid})
    return {"detail": "Message deleted"}


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Delete a conversation and all its messages."""
    try:
        oid = ObjectId(conversation_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    conv = await db.conversations.find_one({"_id": oid, "participants": user_id})
    if not conv:
        raise HTTPException(status_code=403, detail="Not a participant in this conversation")

    await db.messages.delete_many({"conversation_id": conversation_id})
    await db.conversations.delete_one({"_id": oid})
    return {"detail": "Conversation deleted"}


# ─────────────────────────────────────────────────────────────────────────────
# FCM push helper — sends to offline recipients only
# ─────────────────────────────────────────────────────────────────────────────

async def _push_to_eligible_recipients(
    sender_id: str,
    sender_username: str,
    conversation_id: str,
    participant_ids: List[str],
    db,
):
    """
    For every participant who is NOT the sender, fetch their FCM token
    and fire a push notification.

    We push to ALL participants (online and offline) because:
    - If the user has the app open but is on a different screen, they
      SHOULD get a heads-up banner.
    - The Flutter FCM foreground listener suppresses the notification
      automatically when the user is actively viewing that conversation.
    """
    eligible_ids = [pid for pid in participant_ids if pid != sender_id]
    if not eligible_ids:
        return

    object_ids = []
    for pid in eligible_ids:
        try:
            object_ids.append(ObjectId(pid))
        except Exception:
            pass

    if not object_ids:
        return

    cursor = db.users.find(
        {"_id": {"$in": object_ids}, "fcm_token": {"$exists": True, "$ne": None}},
        {"fcm_token": 1},
    )
    async for user_doc in cursor:
        fcm_token = user_doc.get("fcm_token")
        if fcm_token:
            schedule_message_notification(
                fcm_token=fcm_token,
                sender_username=sender_username,
                conversation_id=conversation_id,
            )


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """
    Real-time chat WebSocket.
    Client sends JSON:
      {"type": "message",  "conversation_id": "...", "text": "...",
       "reply_to_id": "...", "reply_to_text": "..."}
      {"type": "typing",   "conversation_id": "..."}
      {"type": "read",     "conversation_id": "..."}
      {"type": "react",    "conversation_id": "...", "message_id": "...", "emoji": "❤️"}
    """
    payload = decode_token(token)
    if not payload:
        await websocket.close(code=1008, reason="Invalid or expired token")
        return

    user_id: str = payload["sub"]
    await manager.connect(websocket, user_id)
    logger.info(f"[WS] User {user_id} connected")

    # Fetch sender profile once per connection (username for notifications)
    db = get_db()
    sender_doc = await db.users.find_one({"_id": ObjectId(user_id)}, {"username": 1})
    sender_username: str = (sender_doc or {}).get("username", "Someone")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data        = json.loads(raw)
                event_type  = data.get("type")
                conv_id     = data.get("conversation_id", "")

                # ── Send message ──────────────────────────────
                if event_type == "message":
                    text               = (data.get("text") or "").strip()
                    encrypted_aes_keys = data.get("encrypted_aes_keys")
                    client_created_at  = _parse_client_created_at(data.get("client_created_at"))
                    reply_to_id        = data.get("reply_to_id")
                    reply_to_text      = data.get("reply_to_text")

                    if not conv_id or not text:
                        continue
                    if len(text) > 10000:
                        continue

                    try:
                        msg_res, participants = await save_message(
                            conv_id,
                            user_id,
                            text,
                            db,
                            encrypted_aes_keys=encrypted_aes_keys,
                            created_at=client_created_at,
                            reply_to_id=reply_to_id,
                            reply_to_text=reply_to_text,
                        )
                    except ValueError as e:
                        logger.warning(f"[WS] save_message error: {e}")
                        continue

                    # 1️⃣  Deliver over WebSocket to all online participants
                    broadcast = json.dumps({
                        "type":    "message",
                        "message": msg_res.model_dump(mode="json"),
                    })
                    for pid in participants:
                        await manager.send_personal_message(broadcast, pid)

                    # 2️⃣  Push FCM to eligible participants (fire & forget)
                    asyncio.create_task(
                        _push_to_eligible_recipients(
                            sender_id=user_id,
                            sender_username=sender_username,
                            conversation_id=conv_id,
                            participant_ids=participants,
                            db=db,
                        )
                    )

                # ── Typing indicator ──────────────────────────
                elif event_type == "typing":
                    if not conv_id:
                        continue
                    try:
                        conv = await db.conversations.find_one(
                            {"_id": ObjectId(conv_id), "participants": user_id}
                        )
                    except InvalidId:
                        continue
                    if not conv:
                        continue

                    typing_msg = json.dumps({
                        "type":            "typing",
                        "conversation_id": conv_id,
                        "user_id":         user_id,
                    })
                    for pid in conv["participants"]:
                        if pid != user_id:
                            await manager.send_personal_message(typing_msg, pid)

                # ── Mark as read ──────────────────────────────
                elif event_type == "read":
                    if conv_id:
                        await mark_messages_read(conv_id, user_id, db)

                # ── React to message ──────────────────────────
                elif event_type == "react":
                    emoji      = (data.get("emoji") or "").strip()
                    message_id = (data.get("message_id") or "").strip()
                    if not conv_id or not emoji or not message_id:
                        continue
                    if len(emoji) > 10:
                        continue

                    try:
                        reactions, _ = await toggle_reaction(message_id, user_id, emoji, db)
                    except ValueError as e:
                        logger.warning(f"[WS] toggle_reaction error: {e}")
                        continue

                    try:
                        conv = await db.conversations.find_one(
                            {"_id": ObjectId(conv_id), "participants": user_id}
                        )
                    except InvalidId:
                        continue
                    if not conv:
                        continue

                    broadcast = json.dumps({
                        "type":            "react",
                        "message_id":      message_id,
                        "conversation_id": conv_id,
                        "reactions":       reactions,
                    })
                    for pid in conv["participants"]:
                        await manager.send_personal_message(broadcast, pid)

            except json.JSONDecodeError:
                logger.debug(f"[WS] Non-JSON message from {user_id}")
            except Exception as e:
                logger.error(f"[WS] Error handling message from {user_id}: {e}")

    except WebSocketDisconnect:
        logger.info(f"[WS] User {user_id} disconnected")
    except Exception as e:
        logger.error(f"[WS] Unexpected error for {user_id}: {e}")
    finally:
        manager.disconnect(websocket, user_id)
