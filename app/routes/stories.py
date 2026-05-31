from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional
import asyncio
import logging

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.database import get_db
from app.limiter import limiter
from app.utils.jwt_handler import get_current_user_id
from app.utils.cloudinary_transform import optimize_image

router = APIRouter()
logger = logging.getLogger(__name__)

_VALID_DURATIONS = {3, 6, 9, 12, 15, 18, 21, 24}


# ─────────────────────────────────────────────────────────────────────────────
# Background cleanup — runs every hour, deletes expired stories from
# Cloudinary AND MongoDB so storage doesn't fill up.
# ─────────────────────────────────────────────────────────────────────────────

async def _delete_from_cloudinary(public_id: str) -> None:
    """Run Cloudinary destroy in a thread pool (SDK is synchronous)."""
    if not public_id:
        return
    try:
        import cloudinary.uploader
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: cloudinary.uploader.destroy(public_id, resource_type="image"),
        )
        logger.info(f"[cleanup] Cloudinary deleted: {public_id}")
    except Exception as e:
        logger.warning(f"[cleanup] Cloudinary delete failed for {public_id}: {e}")


async def _run_cleanup_once() -> int:
    """Delete all expired stories. Returns count of deleted stories."""
    db = get_db()
    if db is None:
        return 0
    now = datetime.now(timezone.utc)
    expired = await db.stories.find(
        {"expires_at": {"$lte": now}},
        {"_id": 1, "public_id": 1},
    ).to_list(length=500)

    if not expired:
        return 0

    # Delete Cloudinary assets concurrently
    await asyncio.gather(
        *[_delete_from_cloudinary(s.get("public_id", "")) for s in expired],
        return_exceptions=True,
    )

    # Bulk delete from MongoDB
    ids = [s["_id"] for s in expired]
    result = await db.stories.delete_many({"_id": {"$in": ids}})
    count = result.deleted_count
    logger.info(f"[cleanup] Removed {count} expired stories")
    return count


async def story_cleanup_loop() -> None:
    """Periodic task: cleans up expired stories every hour."""
    # Small initial delay so startup is not blocked
    await asyncio.sleep(60)
    while True:
        try:
            await _run_cleanup_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[cleanup] Unexpected error: {e}")
        await asyncio.sleep(3600)  # every hour


# ─────────────────────────────────────────────────────────────────────────────
# Serializer
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_story(story: dict, current_user_id: str) -> dict:
    raw_pic   = story.get("user_picture")
    raw_media = story.get("media_url", "")
    return {
        "id":               str(story["_id"]),
        "user_id":          story.get("user_id", ""),
        "user_name":        story.get("user_name", ""),
        "user_username":    story.get("user_username", ""),
        "user_picture":     optimize_image(raw_pic, width=200) if raw_pic else None,
        "media_url":        optimize_image(raw_media, width=1080),
        "public_id":        story.get("public_id", ""),
        "expires_in_hours": story.get("expires_in_hours", 24),
        "expires_at":       story.get("expires_at", datetime.now(timezone.utc)).isoformat(),
        "created_at":       story.get("created_at", datetime.now(timezone.utc)).isoformat(),
        "view_count":       story.get("view_count", 0),
        "viewed":           current_user_id in (story.get("viewers") or []),
        "is_own":           story.get("user_id") == current_user_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /stories/my  ← must be declared before /{story_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/my")
async def get_my_stories(
    user_id: str = Depends(get_current_user_id),
    db       = Depends(get_db),
):
    now     = datetime.now(timezone.utc)
    stories = await db.stories.find(
        {"user_id": user_id, "expires_at": {"$gt": now}},
        projection={
            "_id": 1, "user_id": 1, "user_name": 1, "user_username": 1,
            "user_picture": 1, "media_url": 1, "public_id": 1,
            "expires_in_hours": 1, "expires_at": 1, "created_at": 1,
            "view_count": 1, "viewers": 1,
        },
    ).sort("created_at", -1).to_list(length=50)
    return {"stories": [_serialize_story(s, user_id) for s in stories]}


# ─────────────────────────────────────────────────────────────────────────────
# POST /stories/hide-all-from  ← must be declared before /{story_id}
# ─────────────────────────────────────────────────────────────────────────────

class HideFromBody(BaseModel):
    target_username: str


@router.post("/hide-all-from")
async def hide_all_stories_from(
    body:    HideFromBody,
    user_id: str = Depends(get_current_user_id),
    db       = Depends(get_db),
):
    username = body.target_username.strip().lower()
    target   = await db.users.find_one({"username": username}, {"_id": 1})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target_id = str(target["_id"])
    await db.stories.update_many(
        {"user_id": user_id},
        {"$addToSet": {"hidden_from": target_id}},
    )
    return {"ok": True, "hidden_from_user": body.target_username}


# ─────────────────────────────────────────────────────────────────────────────
# POST /stories/ — Create a story (image only)
# ─────────────────────────────────────────────────────────────────────────────

class CreateStoryBody(BaseModel):
    media_url:        str
    public_id:        str = ""
    expires_in_hours: int = 24   # 6 | 12 | 24


@router.post("/")
@limiter.limit("20/hour")
async def create_story(
    request: Request,
    body:    CreateStoryBody,
    user_id: str = Depends(get_current_user_id),
    db       = Depends(get_db),
):
    if body.expires_in_hours not in _VALID_DURATIONS:
        raise HTTPException(
            status_code=400,
            detail=f"expires_in_hours must be one of {sorted(_VALID_DURATIONS)}",
        )

    if not body.media_url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=400, detail="Invalid media URL. Must be a Cloudinary URL.")

    user = await db.users.find_one(
        {"_id": ObjectId(user_id)},
        {"name": 1, "username": 1, "picture": 1},
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    now        = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=body.expires_in_hours)

    doc = {
        "user_id":          user_id,
        "user_name":        user["name"],
        "user_username":    user["username"],
        "user_picture":     user.get("picture"),
        "media_url":        body.media_url,
        "public_id":        body.public_id,
        "expires_in_hours": body.expires_in_hours,
        "expires_at":       expires_at,
        "created_at":       now,
        "view_count":       0,
        "viewers":          [],
        "hidden_from":      [],
        "is_close_friends": False,
    }

    result    = await db.stories.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize_story(doc, user_id)


# ─────────────────────────────────────────────────────────────────────────────
# GET /stories/ — Story feed (own first, then following/public)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/")
async def get_stories(
    user_id: str = Depends(get_current_user_id),
    db       = Depends(get_db),
):
    now = datetime.now(timezone.utc)

    # People this user follows
    following_docs = await db.follows.find(
        {"follower_id": user_id}, {"following_id": 1, "_id": 0},
    ).to_list(length=2000)
    following_ids = {d["following_id"] for d in following_docs}

    # People who follow this user
    follower_docs = await db.follows.find(
        {"following_id": user_id}, {"follower_id": 1, "_id": 0},
    ).to_list(length=2000)
    follower_ids = {d["follower_id"] for d in follower_docs}
    related_ids  = following_ids | follower_ids | {user_id}

    # All active stories
    all_stories = await db.stories.find(
        {"expires_at": {"$gt": now}},
        projection={
            "_id": 1, "user_id": 1, "user_name": 1, "user_username": 1,
            "user_picture": 1, "media_url": 1, "public_id": 1,
            "expires_in_hours": 1, "expires_at": 1, "created_at": 1,
            "view_count": 1, "viewers": 1, "hidden_from": 1,
        },
    ).sort("created_at", -1).to_list(length=1000)

    # Privacy map for all story authors
    author_ids = list({s["user_id"] for s in all_stories})
    author_privacy: dict = {}
    if author_ids:
        author_docs = await db.users.find(
            {"_id": {"$in": [ObjectId(aid) for aid in author_ids]}},
            {"_id": 1, "is_private": 1},
        ).to_list(length=len(author_ids))
        author_privacy = {str(d["_id"]): bool(d.get("is_private", False)) for d in author_docs}

    # Filter by visibility and privacy
    visible: list = []
    for story in all_stories:
        sid = story["user_id"]
        if user_id in (story.get("hidden_from") or []):
            continue
        if sid == user_id:
            visible.append(story)
            continue
        is_private = author_privacy.get(sid, False)
        if is_private and sid not in related_ids:
            continue
        visible.append(story)

    # Group by user
    grouped: dict = defaultdict(list)
    for story in visible:
        grouped[story["user_id"]].append(story)

    result = []

    # Own stories first
    if user_id in grouped:
        own = sorted(grouped[user_id], key=lambda x: x["created_at"])
        result.append({
            "user_id":       user_id,
            "user_name":     own[0]["user_name"],
            "user_username": own[0]["user_username"],
            "user_picture":  optimize_image(own[0].get("user_picture"), width=200)
                             if own[0].get("user_picture") else None,
            "is_own":        True,
            "all_seen":      False,
            "stories":       [_serialize_story(s, user_id) for s in own],
        })

    # Others sorted by most recent story
    others = [(uid, stories) for uid, stories in grouped.items() if uid != user_id]
    others.sort(key=lambda kv: max(s["created_at"] for s in kv[1]), reverse=True)

    for uid, stories in others:
        ss       = sorted(stories, key=lambda x: x["created_at"])
        all_seen = all(user_id in (s.get("viewers") or []) for s in ss)
        result.append({
            "user_id":       uid,
            "user_name":     ss[0]["user_name"],
            "user_username": ss[0]["user_username"],
            "user_picture":  optimize_image(ss[0].get("user_picture"), width=200)
                             if ss[0].get("user_picture") else None,
            "is_own":        False,
            "all_seen":      all_seen,
            "stories":       [_serialize_story(s, user_id) for s in ss],
        })

    return {"users": result}


# ─────────────────────────────────────────────────────────────────────────────
# POST /stories/{story_id}/view
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{story_id}/view")
async def view_story(
    story_id: str,
    user_id:  str = Depends(get_current_user_id),
    db        = Depends(get_db),
):
    try:
        oid = ObjectId(story_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid story ID")

    story = await db.stories.find_one({"_id": oid}, {"user_id": 1, "viewers": 1})
    if not story or story["user_id"] == user_id:
        return {"ok": True}

    if user_id not in (story.get("viewers") or []):
        await db.stories.update_one(
            {"_id": oid},
            {"$addToSet": {"viewers": user_id}, "$inc": {"view_count": 1}},
        )
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# POST /stories/{story_id}/hide-from
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{story_id}/hide-from")
async def hide_story_from(
    story_id: str,
    body:     HideFromBody,
    user_id:  str = Depends(get_current_user_id),
    db        = Depends(get_db),
):
    try:
        oid = ObjectId(story_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid story ID")

    story = await db.stories.find_one({"_id": oid}, {"user_id": 1})
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    if story["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your story")

    username = body.target_username.strip().lower()
    target   = await db.users.find_one({"username": username}, {"_id": 1})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    await db.stories.update_one(
        {"_id": oid},
        {"$addToSet": {"hidden_from": str(target["_id"])}},
    )
    return {"ok": True, "hidden_from_user": body.target_username}


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /stories/{story_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/{story_id}")
async def delete_story(
    story_id: str,
    user_id:  str = Depends(get_current_user_id),
    db        = Depends(get_db),
):
    try:
        oid = ObjectId(story_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid story ID")

    story = await db.stories.find_one({"_id": oid}, {"user_id": 1})
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    if story["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your story")

    await db.stories.delete_one({"_id": oid})
    return {"ok": True}
