from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query, HTTPException
from typing import List
import json
import logging
from bson import ObjectId
from bson.errors import InvalidId

from app.database import get_db, _db
from app.utils.jwt_handler import get_current_user_id, decode_token
from app.models.chat import ConversationResponse, MessageResponse, ConversationCreate
from app.services.chat_service import (
    manager,
    get_user_conversations,
    get_or_create_conversation,
    get_conversation_messages,
    save_message,
    mark_messages_read,
)

logger = logging.getLogger(__name__)
router = APIRouter()


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

    # Mark as read in background — don't make the client wait
    await mark_messages_read(conversation_id, user_id, db)
    return await get_conversation_messages(conversation_id, db, skip, limit)


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


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """
    Real-time chat WebSocket.
    Client sends JSON: {"type": "message"|"typing"|"read", "conversation_id": "...", "text": "..."}
    """
    # Authenticate
    payload = decode_token(token)
    if not payload:
        await websocket.close(code=1008, reason="Invalid or expired token")
        return

    user_id: str = payload["sub"]
    await manager.connect(websocket, user_id)
    logger.info(f"[WS] User {user_id} connected")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data        = json.loads(raw)
                event_type  = data.get("type")
                conv_id     = data.get("conversation_id", "")

                # ── Send message ──────────────────────────────
                if event_type == "message":
                    text = (data.get("text") or "").strip()
                    if not conv_id or not text:
                        continue

                    try:
                        msg_res, participants = await save_message(
                            conv_id, user_id, text, _db
                        )
                    except ValueError as e:
                        logger.warning(f"[WS] save_message error: {e}")
                        continue

                    # Broadcast to all participants (including sender — confirms delivery)
                    broadcast = json.dumps({
                        "type": "message",
                        "message": msg_res.model_dump(mode="json"),
                    })
                    for pid in participants:
                        await manager.send_personal_message(broadcast, pid)

                # ── Typing indicator ──────────────────────────
                elif event_type == "typing":
                    if not conv_id:
                        continue
                    try:
                        conv = await _db.conversations.find_one(
                            {"_id": ObjectId(conv_id), "participants": user_id}
                        )
                    except InvalidId:
                        continue
                    if not conv:
                        continue

                    typing_msg = json.dumps({
                        "type": "typing",
                        "conversation_id": conv_id,
                        "user_id": user_id,
                    })
                    for pid in conv["participants"]:
                        if pid != user_id:
                            await manager.send_personal_message(typing_msg, pid)

                # ── Mark as read ──────────────────────────────
                elif event_type == "read":
                    if conv_id:
                        await mark_messages_read(conv_id, user_id, _db)

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
