import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.database import get_db
from app.limiter import limiter
from app.services.notification_service import send_follow_push, is_fcm_ready
from app.utils.jwt_handler import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter()

# Compiled once — safe regex, no ReDoS risk
_USERNAME_RE = re.compile(r"^[a-z0-9_.]{3,20}$")


# ─────────────────────────────────────────────────────────────────────────────
# USERNAME AVAILABILITY CHECK  (public — no auth needed, used during signup)
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_username(raw: str) -> str:
    s = raw.strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)       # spaces/hyphens → _
    s = re.sub(r"[^a-z0-9_.]", "", s)    # drop invalid chars
    s = re.sub(r"[_.]{2,}", "_", s)       # collapse __ or ..
    s = s.strip("_.")                      # no leading/trailing _ .
    return s[:20]                          # hard max-length


@router.get("/check-username")
@limiter.limit("30/minute")
async def check_username(
    request: Request,
    username: str = Query(..., min_length=1, max_length=50),
    db=Depends(get_db),
):
    """
    Check if a username is available.
    - No auth required (called before account creation)
    - Sanitizes input server-side
    - Uses unique index on username (IXSCAN, not COLLSCAN)
    - Rate limited: 30/min per IP
    """
    clean = _sanitize_username(username)

    if len(clean) < 3:
        return {
            "success": False,
            "error": "TOO_SHORT",
            "message": "Username must be at least 3 characters",
        }

    if not _USERNAME_RE.match(clean):
        return {
            "success": False,
            "error": "INVALID",
            "message": "Only letters, numbers, _ and . allowed",
        }

    # Indexed lookup — projection: only _id (minimum data fetched)
    existing = await db.users.find_one({"username": clean}, projection={"_id": 1})

    if existing is None:
        return {"success": True, "available": True, "sanitized_username": clean}

    # Taken → generate suggestions
    suggestions = await _build_suggestions(clean, db)
    return {
        "success": True,
        "available": False,
        "sanitized_username": clean,
        "suggestions": suggestions,
    }


async def _build_suggestions(base: str, db) -> list:
    import random
    year = datetime.now(timezone.utc).year
    candidates = []
    for sfx in [str(year % 100), str(year), "hq", "real", "the", "app"]:
        candidates += [f"{base}{sfx}", f"{base}_{sfx}"]
    for n in [str(random.randint(1, 999)) for _ in range(8)]:
        candidates.append(f"{base}{n}")
    candidates += [f"the_{base}", f"im_{base}"]

    seen, unique = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    results = []
    for c in unique:
        if len(results) >= 5:
            break
        if 3 <= len(c) <= 20 and _USERNAME_RE.match(c):
            taken = await db.users.find_one({"username": c}, projection={"_id": 1})
            if not taken:
                results.append(c)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE & SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me")
async def get_me(user_id: str = Depends(get_current_user_id), db=Depends(get_db)):
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id":            str(user["_id"]),
        "name":          user["name"],
        "username":      user["username"],
        "email":         user["email"],
        "picture":       user.get("picture"),
        "is_google_user": user.get("is_google_user", False),
        "created_at":    user["created_at"],
        "public_key":    user.get("public_key"),
    }


class FCMTokenUpdate(BaseModel):
    fcm_token: str


@router.put("/me/fcm-token")
async def update_fcm_token(
    data: FCMTokenUpdate,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"fcm_token": data.fcm_token, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"detail": "FCM token updated"}


class PublicKeyUpdate(BaseModel):
    public_key: str


@router.put("/me/public-key")
async def update_public_key(
    data: PublicKeyUpdate,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"public_key": data.public_key, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"detail": "Public key updated"}


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_users(
    q: str = "",
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    if not q or not q.strip():
        return []

    q          = q.strip()
    escaped_q  = re.escape(q)

    cursor = db.users.find({
        "$and": [
            {"_id": {"$ne": ObjectId(user_id)}},
            {"username": {"$regex": escaped_q, "$options": "i"}},
        ]
    }).limit(20)
    users = await cursor.to_list(length=20)
    if not users:
        return []

    target_ids    = [str(u["_id"]) for u in users]
    following_docs = await db.follows.find(
        {"follower_id": user_id, "following_id": {"$in": target_ids}},
        {"following_id": 1, "_id": 0},
    ).to_list(length=20)
    following_set = {doc["following_id"] for doc in following_docs}

    return [
        {
            "id":          str(u["_id"]),
            "name":        u["name"],
            "username":    u["username"],
            "picture":     u.get("picture"),
            "public_key":  u.get("public_key"),
            "is_following": str(u["_id"]) in following_set,
        }
        for u in users
    ]


# ─────────────────────────────────────────────────────────────────────────────
# FOLLOW / UNFOLLOW / FOLLOW-STATUS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{target_id}/follow")
async def follow_user(
    target_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Follow a user. Idempotent."""
    if target_id == current_user_id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")

    try:
        target_oid = ObjectId(target_id)
        me_oid     = ObjectId(current_user_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    result = await db.follows.update_one(
        {"follower_id": current_user_id, "following_id": target_id},
        {"$setOnInsert": {
            "follower_id":  current_user_id,
            "following_id": target_id,
            "created_at":   datetime.now(timezone.utc),
        }},
        upsert=True,
    )

    if result.upserted_id is None:
        logger.info(f"[FOLLOW] already following {current_user_id}→{target_id}")
        return {"detail": "followed", "following": True}

    me, target, _, __ = await asyncio.gather(
        db.users.find_one({"_id": me_oid},     {"name": 1, "username": 1}),
        db.users.find_one({"_id": target_oid}, {"fcm_token": 1}),
        db.users.update_one({"_id": me_oid},     {"$inc": {"following_count": 1}}),
        db.users.update_one({"_id": target_oid}, {"$inc": {"followers_count": 1}}),
    )

    follower_username = (me  or {}).get("username", "")
    follower_name     = (me  or {}).get("name",     "")
    fcm_token         = (target or {}).get("fcm_token")

    logger.info(
        f"[FOLLOW] {current_user_id}({follower_username}) → {target_id} | "
        f"fcm={'✓' if fcm_token else '✗ MISSING — user must open app'}"
    )

    notif_id = str(ObjectId())
    now      = datetime.now(timezone.utc)
    try:
        await db.notifications.insert_one({
            "_id":           ObjectId(notif_id),
            "recipient_id":  target_id,
            "type":          "follow",
            "from_user_id":  current_user_id,
            "from_username": follower_username,
            "from_name":     follower_name,
            "text":          "started following you",
            "read":          False,
            "created_at":    now,
        })
        logger.info(f"[FOLLOW] ✅ notification saved id={notif_id}")
    except Exception as e:
        logger.error(f"[FOLLOW] ❌ notification insert failed: {e}")

    try:
        from app.services.chat_service import manager
        if target_id in manager.active_connections:
            ws_payload = json.dumps({
                "type": "notification",
                "notification": {
                    "id":            notif_id,
                    "recipient_id":  target_id,
                    "type":          "follow",
                    "from_user_id":  current_user_id,
                    "from_username": follower_username,
                    "from_name":     follower_name,
                    "text":          "started following you",
                    "read":          False,
                    "created_at":    now.isoformat(),
                },
            })
            await manager.send_personal_message(ws_payload, target_id)
            logger.info(f"[FOLLOW] ✅ WS notification delivered to {target_id}")
    except Exception as ws_err:
        logger.warning(f"[FOLLOW] WS error: {ws_err}")

    if fcm_token and is_fcm_ready():
        asyncio.create_task(
            send_follow_push(
                fcm_token=fcm_token,
                follower_username=follower_username,
                follower_name=follower_name,
                notif_id=notif_id,
            )
        )
        logger.info(f"[FOLLOW] 📲 FCM task scheduled for {target_id}")
    else:
        if not fcm_token:
            logger.warning(f"[FOLLOW] ⚠️ No FCM token for {target_id} — push skipped")
        if not is_fcm_ready():
            logger.warning("[FOLLOW] ⚠️ Firebase not initialized — push skipped")

    return {"detail": "followed", "following": True}


@router.delete("/{target_id}/follow")
async def unfollow_user(
    target_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Unfollow a user. Idempotent."""
    try:
        target_oid = ObjectId(target_id)
        me_oid     = ObjectId(current_user_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    result = await db.follows.delete_one(
        {"follower_id": current_user_id, "following_id": target_id}
    )
    if result.deleted_count > 0:
        await asyncio.gather(
            db.users.update_one({"_id": me_oid},     {"$inc": {"following_count": -1}}),
            db.users.update_one({"_id": target_oid}, {"$inc": {"followers_count": -1}}),
        )

    logger.info(f"[UNFOLLOW] {current_user_id}→{target_id}")
    return {"detail": "unfollowed", "following": False}


@router.get("/{target_id}/follow")
async def get_follow_status(
    target_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    exists = await db.follows.find_one(
        {"follower_id": current_user_id, "following_id": target_id}
    )
    return {"following": exists is not None}
