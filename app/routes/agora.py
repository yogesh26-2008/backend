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


def _build_token(channel_name: str, uid: int) -> str:
    """Generate an Agora RTC token using the App Certificate."""
    if not settings.agora_app_certificate:
        # No certificate → return empty string (works only when certificate is disabled in console)
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
    user_id: str = Depends(get_current_user_id),
):
    """
    Generate a short-lived Agora RTC token for voice/video calls.
    Requires authentication. Token is valid for 1 hour.
    """
    if not channel or len(channel) < 3:
        raise HTTPException(status_code=400, detail="Invalid channel name")

    # Security: Channel name must contain the caller's user ID
    # Channel format: trandia_<sorted_uid1>_<sorted_uid2>
    if user_id not in channel:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to join this channel"
        )

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
