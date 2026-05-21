from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.database import get_db
from app.models.user import UserResponse, FCMTokenUpdate
from app.utils.jwt_handler import get_current_user_id
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timezone

from typing import List
import re
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def get_me(user_id: str = Depends(get_current_user_id), db=Depends(get_db)):
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(
        id=str(user["_id"]),
        name=user["name"],
        username=user["username"],
        email=user["email"],
        picture=user.get("picture"),
        is_google_user=user.get("is_google_user", False),
        created_at=user["created_at"],
        public_key=user.get("public_key"),
    )


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

@router.get("/search")
async def search_users(
    q: str = "",
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db)
):
    if not q or not q.strip():
        return []

    q = q.strip()
    escaped_q = re.escape(q)
    logger.info(f"[SEARCH] user_id={user_id} query='{q}'")

    # Match by USERNAME ONLY — no name, no email (prevents gmail-like results)
    query_filter = {
        "$and": [
            {"_id": {"$ne": ObjectId(user_id)}},
            {"username": {"$regex": escaped_q, "$options": "i"}},
        ]
    }

    cursor = db.users.find(query_filter).limit(20)
    users = await cursor.to_list(length=20)
    logger.info(f"[SEARCH] Found {len(users)} users")

    if not users:
        return []

    # Batch-fetch follow statuses in ONE query instead of N queries
    target_ids = [str(u["_id"]) for u in users]
    following_cursor = db.follows.find(
        {"follower_id": user_id, "following_id": {"$in": target_ids}},
        {"following_id": 1, "_id": 0}
    )
    following_docs = await following_cursor.to_list(length=20)
    following_set = {doc["following_id"] for doc in following_docs}

    return [
        {
            "id": str(u["_id"]),
            "name": u["name"],
            "username": u["username"],
            "picture": u.get("picture"),
            "public_key": u.get("public_key"),
            "is_following": str(u["_id"]) in following_set,
        }
        for u in users
    ]


# ─── Follow / Unfollow / Follow-status ───────────────────────────────────────

@router.post("/{target_id}/follow")
async def follow_user(
    target_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Follow a user. Idempotent — calling twice has no extra effect."""
    if target_id == current_user_id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")

    try:
        target_oid = ObjectId(target_id)
        me_oid     = ObjectId(current_user_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    # Upsert the follows record (no duplicate)
    await db.follows.update_one(
        {"follower_id": current_user_id, "following_id": target_id},
        {"$setOnInsert": {
            "follower_id": current_user_id,
            "following_id": target_id,
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )

    # Atomic counter updates
    await db.users.update_one({"_id": me_oid},     {"$inc": {"following_count": 1}})
    await db.users.update_one({"_id": target_oid}, {"$inc": {"followers_count": 1}})

    logger.info(f"[FOLLOW] {current_user_id} → {target_id}")
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
        # Only decrement if a record was actually removed
        await db.users.update_one({"_id": me_oid},     {"$inc": {"following_count": -1}})
        await db.users.update_one({"_id": target_oid}, {"$inc": {"followers_count": -1}})

    logger.info(f"[UNFOLLOW] {current_user_id} → {target_id}")
    return {"detail": "unfollowed", "following": False}


@router.get("/{target_id}/follow")
async def get_follow_status(
    target_id: str,
    current_user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Check whether the current user is following target_id."""
    exists = await db.follows.find_one(
        {"follower_id": current_user_id, "following_id": target_id}
    )
    return {"following": exists is not None}
