import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional
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

    # Filter valid candidates first, then do ONE bulk query instead of N find_one calls
    valid = [c for c in unique if 3 <= len(c) <= 20 and _USERNAME_RE.match(c)][:15]
    if not valid:
        return []

    taken_docs = await db.users.find(
        {"username": {"$in": valid}},
        projection={"username": 1, "_id": 0},
    ).to_list(length=len(valid))
    taken_set = {doc["username"] for doc in taken_docs}

    return [c for c in valid if c not in taken_set][:5]

# ─────────────────────────────────────────────────────────────────────────────
# EMAIL AVAILABILITY CHECK  (public — no auth needed, used during signup)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/check-email")
@limiter.limit("10/minute")
async def check_email(
    request: Request,
    email: str = Query(..., min_length=3, max_length=100),
    db=Depends(get_db),
):
    """
    Check if an email is already registered in MongoDB.
    - No auth required (called during signup to detect orphaned Firebase users)
    - Uses unique index on email (IXSCAN, not COLLSCAN)
    - Rate limited: 10/min per IP
    """
    clean = email.strip().lower()
    existing = await db.users.find_one({"email": clean}, projection={"_id": 1})
    return {"exists": existing is not None}



# ─────────────────────────────────────────────────────────────────────────────
# PROFILE & SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me")
async def get_me(user_id: str = Depends(get_current_user_id), db=Depends(get_db)):
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id":              str(user["_id"]),
        "name":            user["name"],
        "username":        user["username"],
        "email":           user["email"],
        "picture":         user.get("picture"),
        "is_google_user":  user.get("is_google_user", False),
        "created_at":      user["created_at"],
        "public_key":      user.get("public_key"),
        "followers_count": user.get("followers_count", 0),
        "following_count": user.get("following_count", 0),
        "bio":             user.get("bio", ""),
        "link":            user.get("link", ""),
        "snapchat_link":   user.get("snapchat_link", ""),
        "instagram_link":  user.get("instagram_link", ""),
        "whatsapp_link":   user.get("whatsapp_link", ""),
        "facebook_link":   user.get("facebook_link", ""),
        "twitter_link":    user.get("twitter_link", ""),
        "youtube_link":    user.get("youtube_link", ""),
        "location_city":   user.get("location_city", ""),
        "location_public": user.get("location_public", True),
        "location_lat":    user.get("location_lat"),
        "location_lng":    user.get("location_lng"),
    }


class ProfileUpdate(BaseModel):
    name: str = ""
    username: str = ""
    bio: str = ""
    link: str = ""
    snapchat_link: str = ""
    instagram_link: str = ""
    whatsapp_link: str = ""
    facebook_link: str = ""
    twitter_link: str = ""
    youtube_link: str = ""


@router.put("/me")
@limiter.limit("10/minute")
async def update_me(
    request: Request,
    data: ProfileUpdate,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    update_dict: dict = {}

    name = data.name.strip()
    if name:
        update_dict["name"] = name[:100]

    if data.username.strip():
        clean = _sanitize_username(data.username)
        if len(clean) < 3:
            raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
        if not _USERNAME_RE.match(clean):
            raise HTTPException(status_code=400, detail="Invalid username format")
        existing = await db.users.find_one(
            {"username": clean, "_id": {"$ne": ObjectId(user_id)}},
            projection={"_id": 1},
        )
        if existing:
            raise HTTPException(status_code=409, detail="Username already taken")
        update_dict["username"] = clean

    update_dict["bio"]            = data.bio.strip()[:300]
    update_dict["link"]           = data.link.strip()[:500]
    update_dict["snapchat_link"]  = data.snapchat_link.strip()[:500]
    update_dict["instagram_link"] = data.instagram_link.strip()[:500]
    update_dict["whatsapp_link"]  = data.whatsapp_link.strip()[:500]
    update_dict["facebook_link"]  = data.facebook_link.strip()[:500]
    update_dict["twitter_link"]   = data.twitter_link.strip()[:500]
    update_dict["youtube_link"]   = data.youtube_link.strip()[:500]
    update_dict["updated_at"]     = datetime.now(timezone.utc)

    await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": update_dict})
    return {"detail": "Profile updated"}


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


class NotificationSettingsUpdate(BaseModel):
    master:   bool = True
    follows:  bool = True
    likes:    bool = True
    comments: bool = True
    messages: bool = True
    stories:  bool = True
    mentions: bool = True


@router.put("/me/notification-settings")
async def update_notification_settings(
    data: NotificationSettingsUpdate,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {
            "notification_settings": {
                "master":   data.master,
                "follows":  data.follows,
                "likes":    data.likes,
                "comments": data.comments,
                "messages": data.messages,
                "stories":  data.stories,
                "mentions": data.mentions,
            },
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return {"detail": "Notification settings updated"}


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
# LOCATION
# ─────────────────────────────────────────────────────────────────────────────

class LocationUpdate(BaseModel):
    latitude: float
    longitude: float
    city: Optional[str] = ""


class LocationPrivacyUpdate(BaseModel):
    is_public: bool


@router.put("/me/location")
@limiter.limit("10/minute")
async def update_my_location(
    request: Request,
    data: LocationUpdate,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    if not (-90 <= data.latitude <= 90) or not (-180 <= data.longitude <= 180):
        raise HTTPException(status_code=400, detail="Invalid coordinates")
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {
            "location_lat":        data.latitude,
            "location_lng":        data.longitude,
            "location_city":       (data.city or "").strip()[:100],
            "location_updated_at": datetime.now(timezone.utc),
        }},
    )
    return {"detail": "Location updated"}


@router.delete("/me/location")
async def remove_my_location(
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$unset": {"location_lat": "", "location_lng": "", "location_city": ""}},
    )
    return {"detail": "Location removed"}


@router.put("/me/location-privacy")
@limiter.limit("20/minute")
async def update_location_privacy(
    request: Request,
    data: LocationPrivacyUpdate,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"location_public": data.is_public, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"detail": "Location privacy updated"}


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/search")
@limiter.limit("20/minute")
async def search_users(
    request: Request,
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
# PUBLIC USER PROFILE  (any authenticated user can view any other user)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{user_id}")
async def get_user_profile(
    user_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    try:
        oid = ObjectId(user_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    user = await db.users.find_one({"_id": oid})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    follow_doc = await db.follows.find_one(
        {"follower_id": current_user_id, "following_id": user_id},
        projection={"_id": 1},
    )

    return {
        "id":              str(user["_id"]),
        "name":            user["name"],
        "username":        user["username"],
        "picture":         user.get("picture"),
        "public_key":      user.get("public_key"),
        "followers_count": user.get("followers_count", 0),
        "following_count": user.get("following_count", 0),
        "bio":             user.get("bio", ""),
        "link":            user.get("link", ""),
        "snapchat_link":   user.get("snapchat_link", ""),
        "instagram_link":  user.get("instagram_link", ""),
        "whatsapp_link":   user.get("whatsapp_link", ""),
        "facebook_link":   user.get("facebook_link", ""),
        "twitter_link":    user.get("twitter_link", ""),
        "youtube_link":    user.get("youtube_link", ""),
        "is_following":    follow_doc is not None,
        "location_city":   user.get("location_city", "") if user.get("location_public", True) else "",
        "location_public": user.get("location_public", True),
    }


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
        db.users.find_one({"_id": target_oid}, {"fcm_token": 1, "notification_settings": 1}),
        db.users.update_one({"_id": me_oid},     {"$inc": {"following_count": 1}}),
        db.users.update_one({"_id": target_oid}, {"$inc": {"followers_count": 1}}),
    )

    follower_username    = (me     or {}).get("username", "")
    follower_name        = (me     or {}).get("name",     "")
    fcm_token            = (target or {}).get("fcm_token")
    _tns                 = (target or {}).get("notification_settings", {})
    target_notif_master  = _tns.get("master",  True)
    target_notif_follows = _tns.get("follows", True)

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

    if fcm_token and is_fcm_ready() and target_notif_master and target_notif_follows:
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
        if not target_notif_master or not target_notif_follows:
            logger.info(f"[FOLLOW] 🔕 FCM skipped — {target_id} has follow notifications disabled")
        elif not fcm_token:
            logger.warning(f"[FOLLOW] ⚠️ No FCM token for {target_id} — push skipped")
        elif not is_fcm_ready():
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
        # Aggregation pipeline update prevents counters from going below 0
        await asyncio.gather(
            db.users.update_one(
                {"_id": me_oid},
                [{"$set": {"following_count": {"$max": [0, {"$subtract": ["$following_count", 1]}]}}}],
            ),
            db.users.update_one(
                {"_id": target_oid},
                [{"$set": {"followers_count": {"$max": [0, {"$subtract": ["$followers_count", 1]}]}}}],
            ),
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


@router.get("/{user_id}/followers")
async def get_followers(
    user_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    # Find all follow relations where target is user_id
    relations = await db.follows.find({"following_id": user_id}).to_list(length=500)
    follower_ids = [r["follower_id"] for r in relations]
    
    if not follower_ids:
        return []
        
    follower_oids = []
    for fid in follower_ids:
        try:
            follower_oids.append(ObjectId(fid))
        except Exception:
            continue
            
    users = await db.users.find({"_id": {"$in": follower_oids}}).to_list(length=500)
    
    # Check which of these followers the current user is following
    target_ids = [str(u["_id"]) for u in users]
    following_docs = await db.follows.find(
        {"follower_id": current_user_id, "following_id": {"$in": target_ids}},
        {"following_id": 1, "_id": 0},
    ).to_list(length=500)
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


@router.get("/{user_id}/following")
async def get_following(
    user_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    # Find all follow relations where follower is user_id
    relations = await db.follows.find({"follower_id": user_id}).to_list(length=500)
    following_ids = [r["following_id"] for r in relations]
    
    if not following_ids:
        return []
        
    following_oids = []
    for fid in following_ids:
        try:
            following_oids.append(ObjectId(fid))
        except Exception:
            continue
            
    users = await db.users.find({"_id": {"$in": following_oids}}).to_list(length=500)
    
    # Check which of these users the current user is following
    target_ids = [str(u["_id"]) for u in users]
    following_docs = await db.follows.find(
        {"follower_id": current_user_id, "following_id": {"$in": target_ids}},
        {"following_id": 1, "_id": 0},
    ).to_list(length=500)
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
