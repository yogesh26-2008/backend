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
from app.cache import get_cache, set_cache, delete_cache, delete_cache_pattern
from app.utils.cloudinary_transform import optimize_image, optimize_thumbnail, optimize_video

# TTL constants (seconds)
_FEED_TTL   = 90   # home feed first page
_SHOTS_TTL  = 90   # shots first page per section

router = APIRouter()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Background task — fire-and-forget like notification
# ─────────────────────────────────────────────────────────────────────────────

async def _send_like_notification(db, post_id: str, liker_id: str):
    """Fetch all needed data and dispatch FCM + DB notification for a like."""
    print(f"[LIKE] 🔔 _send_like_notification called — post={post_id} liker={liker_id}")
    try:
        post, liker = await asyncio.gather(
            db.posts.find_one({"_id": ObjectId(post_id)}, {"user_id": 1}),
            db.users.find_one({"_id": ObjectId(liker_id)}, {"name": 1, "username": 1}),
        )

        if not post:
            print(f"[LIKE] ❌ post not found: {post_id}")
            return
        if not liker:
            print(f"[LIKE] ❌ liker not found: {liker_id}")
            return

        owner_id = post.get("user_id", "")
        print(f"[LIKE] owner_id={owner_id}  liker_id={liker_id}")

        if str(owner_id) == str(liker_id):
            print(f"[LIKE] ⏭ self-like skipped")
            return

        owner = await db.users.find_one(
            {"_id": ObjectId(str(owner_id))},
            {"fcm_token": 1, "notification_settings": 1},
        )
        if not owner:
            print(f"[LIKE] ❌ owner user not found: {owner_id}")
            return

        liker_name     = liker.get("name", "") or ""
        liker_username = liker.get("username", "") or ""
        fcm_token      = owner.get("fcm_token")
        _ns            = owner.get("notification_settings") or {}
        notif_master   = _ns.get("master", True)
        notif_likes    = _ns.get("likes",  True)

        print(f"[LIKE] liker={liker_username!r}  fcm={'✓' if fcm_token else '✗ MISSING'}  "
              f"fcm_ready={is_fcm_ready()}  master={notif_master}  likes={notif_likes}")

        notif_id = str(ObjectId())
        now      = datetime.now(timezone.utc)

        await db.notifications.insert_one({
            "_id":           ObjectId(notif_id),
            "recipient_id":  str(owner_id),
            "type":          "like",
            "from_user_id":  liker_id,
            "from_username": liker_username,
            "from_name":     liker_name,
            "post_id":       post_id,
            "text":          "liked your post",
            "read":          False,
            "created_at":    now,
        })
        print(f"[LIKE] ✅ notification saved id={notif_id}")

        if not fcm_token:
            print(f"[LIKE] ⚠️  owner has no FCM token — push skipped")
            return
        if not is_fcm_ready():
            print(f"[LIKE] ⚠️  Firebase not initialized — push skipped")
            return
        if not notif_master or not notif_likes:
            print(f"[LIKE] ⚠️  notifications disabled by owner — push skipped")
            return

        asyncio.create_task(
            send_like_push(
                fcm_token=fcm_token,
                liker_name=liker_name,
                liker_username=liker_username,
                post_id=post_id,
                notif_id=notif_id,
            )
        )
        print(f"[LIKE] 📲 FCM task scheduled → owner={owner_id}")

    except Exception as e:
        import traceback
        print(f"[LIKE] ❌ notification error: {e}\n{traceback.format_exc()}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _serialize(post: dict, liked_ids: set) -> dict:
    media_type = post.get("media_type", "image")
    raw_media  = post.get("media_url", "")
    raw_thumb  = post.get("thumbnail_url")
    raw_pic    = post.get("user_picture")

    # Serve optimized URLs — clients get smaller files automatically
    if media_type == "video":
        media_url     = optimize_video(raw_media)
        thumbnail_url = optimize_thumbnail(raw_thumb) if raw_thumb else None
    else:
        media_url     = optimize_image(raw_media, width=720)
        thumbnail_url = optimize_thumbnail(raw_thumb) if raw_thumb else None

    return {
        "id":             str(post["_id"]),
        "user_id":        post.get("user_id", ""),
        "user_name":      post.get("user_name", ""),
        "user_username":  post.get("user_username", ""),
        "user_picture":   optimize_image(raw_pic, width=200) if raw_pic else None,
        "media_url":      media_url,
        "thumbnail_url":  thumbnail_url,
        "public_id":      post.get("public_id", ""),
        "media_type":     media_type,
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
    # ── Redis cache for first page only (cursor-less requests) ───────────────
    cache_key = f"feed:u:{user_id}:page1:l{limit}" if not cursor else None
    if cache_key:
        cached = await get_cache(cache_key)
        if cached:
            return cached

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
        empty = {"posts": [], "next_cursor": None}
        if cache_key:
            await set_cache(cache_key, empty, expire_seconds=30)
        return empty

    post_ids = [str(p["_id"]) for p in posts]
    likes = await db.post_likes.find(
        {"user_id": user_id, "post_id": {"$in": post_ids}},
        {"post_id": 1, "_id": 0},
    ).to_list(length=len(post_ids))
    liked_ids = {l["post_id"] for l in likes}

    result = {
        "posts":       [_serialize(p, liked_ids) for p in posts],
        "next_cursor": str(posts[-1]["_id"]) if len(posts) == limit else None,
    }

    if cache_key:
        await set_cache(cache_key, result, expire_seconds=_FEED_TTL)

    return result


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

    # Invalidate this user's first-page feed cache so new post appears immediately
    await delete_cache_pattern(f"feed:u:{user_id}:*")
    if body.section:
        await delete_cache_pattern(f"shots:u:{user_id}:{body.section}:*")

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

    # ── Redis cache for first page only ──────────────────────────────────────
    cache_key = f"shots:u:{user_id}:{section}:page1:l{limit}" if not cursor else None
    if cache_key:
        cached = await get_cache(cache_key)
        if cached:
            return cached

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
        empty = {"posts": [], "next_cursor": None}
        if cache_key:
            await set_cache(cache_key, empty, expire_seconds=30)
        return empty

    post_ids = [str(p["_id"]) for p in posts]
    likes = await db.post_likes.find(
        {"user_id": user_id, "post_id": {"$in": post_ids}},
        {"post_id": 1, "_id": 0},
    ).to_list(length=len(post_ids))
    liked_ids = {l["post_id"] for l in likes}

    result = {
        "posts":       [_serialize(p, liked_ids) for p in posts],
        "next_cursor": str(posts[-1]["_id"]) if len(posts) == limit else None,
    }

    if cache_key:
        await set_cache(cache_key, result, expire_seconds=_SHOTS_TTL)

    return result


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


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /posts/{post_id} — Delete own post (owner only)
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/{post_id}")
async def delete_post(
    post_id: str,
    user_id: str = Depends(get_current_user_id),
    db          = Depends(get_db),
):
    try:
        oid = ObjectId(post_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid post ID")

    post = await db.posts.find_one({"_id": oid}, {"user_id": 1})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if post.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You can only delete your own posts")

    section = post.get("section")
    await db.posts.delete_one({"_id": oid})
    await asyncio.gather(
        db.post_likes.delete_many({"post_id": post_id}),
        db.notifications.delete_many({"post_id": post_id}),
    )

    # Invalidate feed cache for this user
    await delete_cache_pattern(f"feed:u:{user_id}:*")
    if section:
        await delete_cache_pattern(f"shots:u:{user_id}:{section}:*")

    logger.info(f"[POST] deleted id={post_id} user={user_id}")
    return {"deleted": True}
