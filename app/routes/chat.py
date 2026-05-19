from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query, HTTPException
from typing import List
import json
from bson import ObjectId

from app.database import get_db, _db
from app.utils.jwt_handler import get_current_user_id, decode_token
from app.models.chat import ConversationResponse, MessageResponse, ConversationCreate
from app.services.chat_service import (
    manager,
    get_user_conversations,
    get_or_create_conversation,
    get_conversation_messages,
    save_message,
    mark_messages_read
)

router = APIRouter()

@router.get("/conversations", response_model=List[ConversationResponse])
async def list_conversations(user_id: str = Depends(get_current_user_id), db=Depends(get_db)):
    """Get all conversations for the current user."""
    return await get_user_conversations(user_id, db)

@router.post("/conversations")
async def start_conversation(data: ConversationCreate, user_id: str = Depends(get_current_user_id), db=Depends(get_db)):
    """Get or create a 1-on-1 conversation with a user."""
    try:
        conv_id = await get_or_create_conversation(user_id, data.participant_username, db)
        return {"conversation_id": conv_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/{conversation_id}/messages", response_model=List[MessageResponse])
async def get_messages(conversation_id: str, skip: int = 0, limit: int = 50, user_id: str = Depends(get_current_user_id), db=Depends(get_db)):
    """Get messages for a specific conversation."""
    # Ensure user is part of the conversation
    conv = await db.conversations.find_one({"_id": ObjectId(conversation_id), "participants": user_id})
    if not conv:
        raise HTTPException(status_code=403, detail="Not a participant in this conversation")
    
    # Mark messages as read since we are fetching them
    await mark_messages_read(conversation_id, user_id, db)

    return await get_conversation_messages(conversation_id, db, skip, limit)

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    # Authenticate WebSocket connection via token query parameter
    payload = decode_token(token)
    if not payload:
        await websocket.close(code=1008, reason="Invalid token")
        return
    user_id = payload["sub"]

    await manager.connect(websocket, user_id)
    try:
        while True:
            # We expect JSON messages: {"type": "message", "conversation_id": "...", "text": "..."}
            # Or {"type": "typing", "conversation_id": "..."}
            # Or {"type": "read", "conversation_id": "..."}
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                event_type = payload.get("type")
                conversation_id = payload.get("conversation_id")
                
                if event_type == "message":
                    text = payload.get("text")
                    if conversation_id and text:
                        msg_res, participants = await save_message(conversation_id, user_id, text, _db)
                        
                        # Broadcast to all participants (including sender to confirm receipt)
                        broadcast_data = {
                            "type": "message",
                            "message": msg_res.model_dump(mode="json")
                        }
                        broadcast_str = json.dumps(broadcast_data)
                        
                        for pid in participants:
                            await manager.send_personal_message(broadcast_str, pid)
                
                elif event_type == "typing":
                    # Broadcast typing status to other participants
                    conv = await _db.conversations.find_one({"_id": ObjectId(conversation_id), "participants": user_id})
                    if conv:
                        broadcast_str = json.dumps({
                            "type": "typing",
                            "conversation_id": conversation_id,
                            "user_id": user_id
                        })
                        for pid in conv["participants"]:
                            if pid != user_id:
                                await manager.send_personal_message(broadcast_str, pid)
                                
                elif event_type == "read":
                    # Mark as read
                    await mark_messages_read(conversation_id, user_id, _db)
                    
            except json.JSONDecodeError:
                pass
            except Exception as e:
                print(f"WebSocket error processing message: {e}")
                
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
