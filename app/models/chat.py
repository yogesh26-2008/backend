from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime
from app.models.user import UserResponse

class MessageCreate(BaseModel):
    conversation_id: str
    text: str

class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    sender_id: str
    text: str
    created_at: datetime
    read_by: List[str] = []
    encrypted_aes_keys: Dict[str, str] = {}

class ConversationCreate(BaseModel):
    participant_username: str

class ConversationResponse(BaseModel):
    id: str
    participants: List[UserResponse]
    last_message: Optional[str] = None
    last_message_time: Optional[datetime] = None
    unread_counts: Dict[str, int] = {}
    is_group: bool = False
    name: Optional[str] = None
    last_message_encrypted_aes_keys: Dict[str, str] = {}

class ChatEvent(BaseModel):
    type: str # 'message', 'typing', 'read'
    data: dict
