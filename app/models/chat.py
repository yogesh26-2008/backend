from pydantic import BaseModel, field_serializer
from typing import Optional, List, Dict
from datetime import datetime, timezone
from app.models.user import UserResponse


def _to_utc_iso(dt: datetime) -> str:
    """Always returns UTC ISO 8601 string with Z suffix so Flutter can parse correctly."""
    if dt.tzinfo is None:
        # naive datetime — assume UTC (MongoDB stores UTC by default)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


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
    reactions: Dict[str, List[str]] = {}   # emoji -> [user_ids]
    reply_to_id: Optional[str] = None
    reply_to_text: Optional[str] = None   # plain-text preview (sender's copy)

    @field_serializer('created_at')
    def serialize_created_at(self, dt: datetime, _info) -> str:
        return _to_utc_iso(dt)


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

    @field_serializer('last_message_time')
    def serialize_last_message_time(self, dt: Optional[datetime], _info) -> Optional[str]:
        if dt is None:
            return None
        return _to_utc_iso(dt)


class ChatEvent(BaseModel):
    type: str  # 'message', 'typing', 'read', 'react'
    data: dict
