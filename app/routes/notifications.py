"""
notifications.py
─────────────────────────────────────────────────────────────────────────────
REST endpoints for the in-app notification feed.

  GET  /notifications           → paginated list for current user
  PUT  /notifications/read-all  → mark every notification as read
  PUT  /notifications/{id}/read → mark one notification as read
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from datetime import datetime
from app.database import get_db
from app.utils.jwt_handler import get_current_user_id
from app.cache import get_cache, set_cache, delete_cache
from bson import ObjectId
from bson.errors import InvalidId
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def get_notifications(
    cursor: Optional[str] = Query(None, description="Last seen notification _id for cursor pagination"),
    skip: int = Query(0, ge=0, description="Deprecated — use cursor instead"),
    limit: int = Query(40, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Return notifications for the current user, newest first (cursor-paginated)."""
    # Cache the default first page only (no cursor, no skip, default limit).
    cache_key = f"notifs:u:{user_id}" if (not cursor and skip == 0 and limit == 40) else None
    if cache_key:
        cached = await get_cache(cache_key)
        if cached is not None:
            return cached

    query: dict = {"recipient_id": user_id}
    if cursor:
        try:
            query["_id"] = {"$lt": ObjectId(cursor)}
        except Exception:
            pass  # bad cursor — ignore and fetch from start

    find_cursor = db.notifications.find(query).sort("_id", -1)
    if not cursor:
        # Legacy skip-based fallback for old clients
        find_cursor = find_cursor.skip(skip)
    find_cursor = find_cursor.limit(limit)
    docs = await find_cursor.to_list(length=limit)

    # Collect unique sender IDs so we can batch-fetch their pictures
    sender_ids = list({
        d["from_user_id"] for d in docs
        if d.get("from_user_id")
    })

    picture_map: dict = {}
    if sender_ids:
        try:
            from bson import ObjectId as _ObjId
            users = await db.users.find(
                {"_id": {"$in": [_ObjId(uid) for uid in sender_ids if len(uid) == 24]}},
                {"picture": 1},
            ).to_list(length=len(sender_ids))
            picture_map = {str(u["_id"]): u.get("picture") for u in users}
        except Exception:
            pass

    result = []
    for d in docs:
        fuid = d.get("from_user_id") or ""
        result.append({
            "id": str(d["_id"]),
            "type": d.get("type", "follow"),
            "from_user_id": fuid,
            "from_username": d.get("from_username", ""),
            "from_name": d.get("from_name", ""),
            "from_picture": picture_map.get(fuid),
            "text": d.get("text", ""),
            "read": d.get("read", False),
            "created_at": (
                d["created_at"].isoformat()
                if isinstance(d.get("created_at"), datetime)
                else (d.get("created_at") or "")
            ),
        })

    if cache_key:
        await set_cache(cache_key, result, expire_seconds=15)
    return result


@router.put("/read-all")
async def mark_all_read(
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Mark all notifications as read for the current user."""
    result = await db.notifications.update_many(
        {"recipient_id": user_id, "read": False},
        {"$set": {"read": True}},
    )
    await delete_cache(f"notifs:u:{user_id}")
    return {"detail": "ok", "modified": result.modified_count}


@router.put("/{notification_id}/read")
async def mark_one_read(
    notification_id: str,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Mark a single notification as read."""
    try:
        oid = ObjectId(notification_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid notification ID")

    result = await db.notifications.update_one(
        {"_id": oid, "recipient_id": user_id},
        {"$set": {"read": True}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    await delete_cache(f"notifs:u:{user_id}")
    return {"detail": "ok"}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    """Permanently delete a notification owned by the current user."""
    try:
        oid = ObjectId(notification_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid notification ID")

    result = await db.notifications.delete_one(
        {"_id": oid, "recipient_id": user_id},
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    await delete_cache(f"notifs:u:{user_id}")
    return {"detail": "ok"}
