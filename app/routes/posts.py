import asyncio
from datetime import datetime, timezone
from typing import Any, List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
import logging

from app.database import get_db
from app.limiter import limiter
from app.utils.jwt_handler import get_current_user_id
from app.services.notification_service import send_like_push, is_fcm_ready

router = APIRouter()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Background task — fire-and-forget like notification
# ─────────────────────────────────────────────────────────────────────────────

async def _send_like_notification(db, post_id: str, liker_id: str):
    """Fetch all needed data and dispatch FCM + DB notification for a like."""
    try:
        post, liker = await asyncio.gather(
            db.posts.find_one(
                {"_id": ObjectId(post_id)},
                {"user_id": 1},
            ),
            db.users.find_one(
                {"_id": ObjectId(liker_id)},
                {"name": 1, "username": 1},
            ),
        )

        if not post or not liker:
            return

        owner_id = post.get("user_id", "")
        if owner_id == liker_id:
            return  # don't notify yourself

        owner = await db.users.find_one(
            {"_id": ObjectId(owner_id)},
            {"fcm_token": 1, "notification_settings": 1},
        )
        if not owner:
            return

        liker_name     = liker.get("name", "")
        liker_username = liker.get("username", "")
        fcm_token      = owner.get("fcm_token")
        _ns            = owner.get("notification_settings", {})
        notif_master   = _ns.get("master", True)
        notif_likes    = _ns.get("likes",  True)

        notif_id = str(ObjectId())
        now      = datetime.now(timezone.utc)

        await db.notifications.insert_one({
            "_id":           ObjectId(notif_id),
            "recipient_id":  owner_id,
            "type":          "like",
            "from_user_id":  liker_id,
            "from_username": liker_username,
            "from_name":     liker_name,
            "post_id":       post_id,
            "text":          "liked your post",
            "read":          False,
            "created_at":    now,
        })
        logger.info(f"[LIKE] ✅ notification saved id={notif_id}")

        if fcm_token and is_fcm_ready() and notif_master and notif_likes:
            asyncio.create_task(
                send_like_push(
                    fcm_token=fcm_token,
                    liker_name=liker_name,
                    liker_username=liker_username,
                    post_id=post_id,
                    notif_id=notif_id,
                )
            )
            logger.info(f"[LIKE] 📲 FCM task scheduled → owner={owner_id}")

    except Exception as e:
        logger.error(f"[LIKE] ❌ notification error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _serialize(post: dict, liked_ids: set) -> dict:
    return {
        "id":             str(post["_id"]),
        "user_id":        post.get("user_id", ""),
        "user_name":      post.get("user_name", ""),
        "user_username":  post.get("user_username", ""),
        "user_picture":   post.get("user_picture"),
        "media_url":      post.get("media_url", ""),
        "thumbnail_url":  post.get("thumbnail_url"),
        "public_id":      post.get("public_id", ""),
        "media_type":     post.get("media_type", "image"),
        "caption":        post.get("caption", ""),
        "aspect_ratio":   post.get("aspect_ratio", 1.0),
        "section":        post.get("section"),
        "likes_count":    post.get("likes_count", 0),
        "comments_count": post.get("comments_count", 0),
        "is_liked":       str(post["_id"]) in liked_ids,
        "created_at":     post.get("created_at", datetime.now(timezone.utc)).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /posts/ — Feed (cursor-based pagination)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/")
async def get_posts(
    cursor: Optional[str] = Query(None, description="Last post _id for pagination"),
    limit: int            = Query(20, ge=1, le=50),
    user_id: str          = Depends(get_current_user_id),
    db                    = Depends(get_db),
):
    query: dict = {}
    if cursor:
        try:
            query["_id"] = {"$lt": ObjectId(cursor)}
        except (InvalidId, Exception):
            pass

    posts = (
        await db.posts.find(query, projection={
            "_id": 1, "user_id": 1, "user_name": 1, "user_username": 1,
            "user_picture": 1, "media_url": 1, "thumbnail_url": 1, "public_id": 1,
            "media_type": 1, "caption": 1, "aspect_ratio": 1, "section": 1,
            "likes_count": 1, "comments_count": 1, "created_at": 1,
        })
        .sort("_id", -1)
        .limit(limit)
        .to_list(length=limit)
    )

    if not posts:
        return {"posts": [], "next_cursor": None}

    post_ids = [str(p["_id"]) for p in posts]
    likes = await db.post_likes.find(
        {"user_id": user_id, "post_id": {"$in": post_ids}},
        {"post_id": 1, "_id": 0},
    ).to_list(length=len(post_ids))
    liked_ids = {l["post_id"] for l in likes}

    return {
        "posts":       [_serialize(p, liked_ids) for p in posts],
        "next_cursor": str(posts[-1]["_id"]) if len(posts) == limit else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /posts/ — Create a post
# ─────────────────────────────────────────────────────────────────────────────

class CreatePostBody(BaseModel):
    media_url:     str
    thumbnail_url: Optional[str] = None
    public_id:     str = ""
    media_type:    str = "image"   # "image" | "video"
    caption:       str = ""
    aspect_ratio:  float = 1.0
    section:       Optional[str] = None   # "fun" | "learn"


@router.post("/")
@limiter.limit("10/minute")
async def create_post(
    request: Request,
    body: CreatePostBody,
    user_id: str = Depends(get_current_user_id),
    db          = Depends(get_db),
):
    if body.media_type not in ("image", "video"):
        raise HTTPException(status_code=400, detail="media_type must be 'image' or 'video'")

    if not body.media_url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=400, detail="Invalid media URL. Must be a Cloudinary URL.")

    user = await db.users.find_one(
        {"_id": ObjectId(user_id)},
        {"name": 1, "username": 1, "picture": 1},
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    doc = {
        "user_id":        user_id,
        "user_name":      user["name"],
        "user_username":  user["username"],
        "user_picture":   user.get("picture"),
        "media_url":      body.media_url,
        "thumbnail_url":  body.thumbnail_url,
        "public_id":      body.public_id,
        "media_type":     body.media_type,
        "caption":        body.caption.strip()[:2000],
        "aspect_ratio":   max(0.3, min(3.0, body.aspect_ratio)),
        "section":        body.section,
        "likes_count":    0,
        "comments_count": 0,
        "created_at":     datetime.now(timezone.utc),
    }

    result = await db.posts.insert_one(doc)
    doc["_id"] = result.inserted_id
    logger.info(f"[POST] created id={result.inserted_id} user={user_id}")
    return _serialize(doc, set())


# ─────────────────────────────────────────────────────────────────────────────
# GET /posts/shots/ — Shots feed filtered by section ("fun" | "learn")
# Must be declared BEFORE /{post_id}/like to avoid path-param collision
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/shots/")
async def get_shots_feed(
    section: str           = Query(..., description="'fun' or 'learn'"),
    cursor:  Optional[str] = Query(None),
    limit:   int           = Query(10, ge=1, le=20),
    user_id: str           = Depends(get_current_user_id),
    db                     = Depends(get_db),
):
    if section not in ("fun", "learn"):
        raise HTTPException(status_code=400, detail="section must be 'fun' or 'learn'")

    query: dict = {"media_type": "video", "section": section}
    if cursor:
        try:
            query["_id"] = {"$lt": ObjectId(cursor)}
        except Exception:
            pass

    posts = (
        await db.posts.find(query, projection={
            "_id": 1, "user_id": 1, "user_name": 1, "user_username": 1,
            "user_picture": 1, "media_url": 1, "thumbnail_url": 1, "public_id": 1,
            "media_type": 1, "caption": 1, "aspect_ratio": 1, "section": 1,
            "likes_count": 1, "comments_count": 1, "created_at": 1,
        })
        .sort("_id", -1)
        .limit(limit)
        .to_list(length=limit)
    )

    if not posts:
        return {"posts": [], "next_cursor": None}

    post_ids = [str(p["_id"]) for p in posts]
    likes = await db.post_likes.find(
        {"user_id": user_id, "post_id": {"$in": post_ids}},
        {"post_id": 1, "_id": 0},
    ).to_list(length=len(post_ids))
    liked_ids = {l["post_id"] for l in likes}

    return {
        "posts":       [_serialize(p, liked_ids) for p in posts],
        "next_cursor": str(posts[-1]["_id"]) if len(posts) == limit else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /posts/{id}/like  &  DELETE /posts/{id}/like — Toggle like
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/{post_id}/like")
async def like_post(
    post_id: str,
    user_id: str = Depends(get_current_user_id),
    db           = Depends(get_db),
):
    try:
        oid = ObjectId(post_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid post ID")

    res = await db.post_likes.update_one(
        {"post_id": post_id, "user_id": user_id},
        {"$setOnInsert": {
            "post_id":    post_id,
            "user_id":    user_id,
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    if res.upserted_id:
        await db.posts.update_one({"_id": oid}, {"$inc": {"likes_count": 1}})
        asyncio.create_task(_send_like_notification(db, post_id, user_id))
    return {"liked": True}


@router.delete("/{post_id}/like")
async def unlike_post(
    post_id: str,
    user_id: str = Depends(get_current_user_id),
    db           = Depends(get_db),
):
    try:
        oid = ObjectId(post_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid post ID")

    res = await db.post_likes.delete_one({"post_id": post_id, "user_id": user_id})
    if res.deleted_count > 0:
        await db.posts.update_one(
            {"_id": oid},
            [{"$set": {"likes_count": {"$max": [0, {"$subtract": ["$likes_count", 1]}]}}}],
        )
    return {"liked": False}


# ─────────────────────────────────────────────────────────────────────────────
# GET /posts/user/{user_id} — Posts by a specific user
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/user/{target_user_id}")
async def get_user_posts(
    target_user_id: str,
    cursor:  Optional[str] = Query(None),
    limit:   int           = Query(20, ge=1, le=50),
    user_id: str           = Depends(get_current_user_id),
    db                     = Depends(get_db),
):
    query: dict = {"user_id": target_user_id}
    if cursor:
        try:
            query["_id"] = {"$lt": ObjectId(cursor)}
        except Exception:
            pass

    posts = (
        await db.posts.find(query)
        .sort("_id", -1)
        .limit(limit)
        .to_list(length=limit)
    )
    if not posts:
        return {"posts": [], "next_cursor": None}

    post_ids = [str(p["_id"]) for p in posts]
    likes = await db.post_likes.find(
        {"user_id": user_id, "post_id": {"$in": post_ids}},
        {"post_id": 1, "_id": 0},
    ).to_list(length=len(post_ids))
    liked_ids = {l["post_id"] for l in likes}

    return {
        "posts":       [_serialize(p, liked_ids) for p in posts],
        "next_cursor": str(posts[-1]["_id"]) if len(posts) == limit else None,
    }
