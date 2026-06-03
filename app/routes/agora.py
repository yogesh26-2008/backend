# routes/agora.py
# Agora RTC Token generation endpoint.
# GET /agora/token?channel=xxx&uid=0  → { "token": "...", "app_id": "..." }

import time
from fastapi import APIRouter, Depends, HTTPException, Query

from agora_token_builder import RtcTokenBuilder
from agora_token_builder.RtcTokenBuilder import Role_Publisher

from app.config import settings
from app.utils.jwt_handler import get_current_user_id

router = APIRouter()

# Token valid for 1 hour (3600 seconds)
_TOKEN_EXPIRE_SECONDS = 3600

# Channel format: trandia_{user_id_1}_{user_id_2}
# MongoDB ObjectIds are 24 hex chars each.
_CHANNEL_PREFIX = "trandia_"


def _extract_participants(channel: str) -> tuple[str, str] | None:
    """
    Parse channel name 'trandia_{uid1}_{uid2}' and return (uid1, uid2).
    Returns None if the format is invalid.
    """
    if not channel.startswith(_CHANNEL_PREFIX):
        return None
    rest = channel[len(_CHANNEL_PREFIX):]
    # MongoDB ObjectId = 24 hex chars; expect exactly two joined by '_'
    parts = rest.split("_")
    if len(parts) != 2:
        return None
    uid1, uid2 = parts
    if len(uid1) != 24 or len(uid2) != 24:
        return None
    return uid1, uid2


def _build_token(channel_name: str, uid: int) -> str:
    """Generate an Agora RTC token using the App Certificate."""
    if not settings.agora_app_certificate:
        # Certificate disabled in Agora console → empty token works
        return ""

    expire_timestamp = int(time.time()) + _TOKEN_EXPIRE_SECONDS

    token = RtcTokenBuilder.buildTokenWithUid(
        appId=settings.agora_app_id,
        appCertificate=settings.agora_app_certificate,
        channelName=channel_name,
        uid=uid,
        role=Role_Publisher,
        privilegeExpiredTs=expire_timestamp,
    )
    return token


@router.get("/token")
async def get_agora_token(
    channel: str = Query(..., description="Agora channel name"),
    uid: int = Query(default=0, description="Agora user UID (0 = auto-assign)"),
    current_user_id: str = Depends(get_current_user_id),
):
    """
    Generate a short-lived Agora RTC token for voice/video calls.
    Caller must be an authenticated participant of the requested channel.
    Token is valid for 1 hour.
    """
    if not channel or len(channel) < 3:
        raise HTTPException(status_code=400, detail="Invalid channel name")

    participants = _extract_participants(channel)
    if participants is None:
        raise HTTPException(status_code=400, detail="Invalid channel name format")

    uid1, uid2 = participants
    if current_user_id not in (uid1, uid2):
        raise HTTPException(status_code=403, detail="You are not a participant in this call")

    try:
        token = _build_token(channel, uid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token generation failed: {e}")

    return {
        "token": token,
        "app_id": settings.agora_app_id,
        "channel": channel,
        "uid": uid,
        "expires_in": _TOKEN_EXPIRE_SECONDS,
    }
