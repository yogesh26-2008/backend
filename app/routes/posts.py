import asyncio
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
import logging

from app.database import get_db
from app.limiter import limiter
from app.utils.jwt_handler import get_current_user_id
from app.services.notification_service import send_like_push, send_comment_push, is_fcm_ready
from app.cache import get_cache, set_cache, delete_cache_pattern
from app.utils.cloudinary_transform import optimize_image, optimize_thumbnail, optimize_video
from app.task_queue import task_queue

# TTL constants (seconds)
_FEED_TTL   = 300  # home feed first page (5 min)
_SHOTS_TTL  = 300  # shots first page per section (5 min)

router = APIRouter()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Background task — fire-and-forget like notification
# ─────────────────────────────────────────────────────────────────────────────

async def _send_like_notification(db, post_id: str, liker_id: str):
    """Fetch all needed data and dispatch FCM + DB notification for a like."""
    logger.debug("[LIKE] _send_like_notification called")
    try:
        post, liker = await asyncio.gather(
            db.posts.find_one({"_id": ObjectId(post_id)}, {"user_id": 1}),
            db.users.find_one({"_id": ObjectId(liker_id)}, {"name": 1, "username": 1}),
        )

        if not post:
            logger.warning("[LIKE] post not found")
            return
        if not liker:
            logger.warning("[LIKE] liker not found")
            return

        owner_id = post.get("user_id", "")
        logger.debug("[LIKE] owner and liker resolved")

        if str(owner_id) == str(liker_id):
            logger.debug("[LIKE] self-like skipped")
            return

        owner = await db.users.find_one(
            {"_id": ObjectId(str(owner_id))},
            {"fcm_token": 1, "notification_settings": 1},
        )
        if not owner:
            logger.warning("[LIKE] owner user not found")
            return

        liker_name     = liker.get("name", "") or ""
        liker_username = liker.get("username", "") or ""
        fcm_token      = owner.get("fcm_token")
        _ns            = owner.get("notification_settings") or {}
        notif_master   = _ns.get("master", True)
        notif_likes    = _ns.get("likes",  True)

        logger.debug(f"[LIKE] fcm_ready={is_fcm_ready()} master={notif_master} likes={notif_likes}")

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
        logger.info("[LIKE] notification saved")

        if not fcm_token:
            logger.debug("[LIKE] owner has no FCM token — push skipped")
            return
        if not is_fcm_ready():
            logger.debug("[LIKE] Firebase not initialized — push skipped")
            return
        if not notif_master or not notif_likes:
            logger.debug("[LIKE] notifications disabled — push skipped")
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
        logger.info("[LIKE] FCM push queued")

    except Exception as e:
        logger.error(f"[LIKE] notification error: {e}", exc_info=True)


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
        media_url     = optimize_image(raw_media, width=540)
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

    # ── Build block list (both directions) ───────────────────────────────────
    # Users blocked by me
    blocked_by_me_docs = await db.blocks.find(
        {"blocker_id": user_id}, {"blocked_id": 1, "_id": 0}
    ).to_list(length=500)
    blocked_by_me = {d["blocked_id"] for d in blocked_by_me_docs}

    # Users who blocked me
    blocked_me_docs = await db.blocks.find(
        {"blocked_id": user_id}, {"blocker_id": 1, "_id": 0}
    ).to_list(length=500)
    blocked_me = {d["blocker_id"] for d in blocked_me_docs}

    all_blocked = blocked_by_me | blocked_me

    # ── Build followees list ──────────────────────────────────────────────────
    followee_docs = await db.follows.find(
        {"follower_id": user_id}, {"following_id": 1, "_id": 0}
    ).to_list(length=2000)
    followee_ids = [d["following_id"] for d in followee_docs]

    # ── Feed query ────────────────────────────────────────────────────────────
    query: dict = {}

    if followee_ids:
        # Personalised: posts from self + people I follow, minus blocked
        visible_ids = list({user_id} | set(followee_ids) - all_blocked)
        query["user_id"] = {"$in": visible_ids}
    else:
        # Discovery fallback: recent posts from everyone, minus blocked
        if all_blocked:
            query["user_id"] = {"$nin": list(all_blocked)}

    # Cursor-based pagination
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
@limiter.limit("60/minute")
async def like_post(
    request: Request,
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
        await task_queue.enqueue(_send_like_notification, db, post_id, user_id)
    return {"liked": True}


@router.delete("/{post_id}/like")
@limiter.limit("60/minute")
async def unlike_post(
    request: Request,
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
# GET /posts/{post_id} — Fetch a single post
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{post_id}")
async def get_post(
    post_id: str,
    user_id: str = Depends(get_current_user_id),
    db           = Depends(get_db),
):
    try:
        oid = ObjectId(post_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid post ID")

    post = await db.posts.find_one({"_id": oid})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    likes = await db.post_likes.find_one({"user_id": user_id, "post_id": post_id})
    liked_ids = {post_id} if likes else set()

    return _serialize(post, liked_ids)


# ─────────────────────────────────────────────────────────────────────────────
# GET /posts/{post_id}/likers — Users who liked a post
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/{post_id}/likers")
async def get_post_likers(
    post_id: str,
    skip:    int = Query(0, ge=0),
    limit:   int = Query(30, ge=1, le=50),
    user_id: str = Depends(get_current_user_id),
    db           = Depends(get_db),
):
    try:
        ObjectId(post_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid post ID")

    like_docs = await db.post_likes.find(
        {"post_id": post_id},
        {"user_id": 1, "_id": 0},
    ).skip(skip).limit(limit).to_list(length=limit)

    if not like_docs:
        return []

    liker_ids = [d["user_id"] for d in like_docs]
    users = await db.users.find(
        {"_id": {"$in": [ObjectId(uid) for uid in liker_ids]}},
        {"name": 1, "username": 1, "picture": 1},
    ).to_list(length=limit)

    following_docs = await db.follows.find(
        {"follower_id": user_id, "following_id": {"$in": liker_ids}},
        {"following_id": 1, "_id": 0},
    ).to_list(length=limit)
    following_set = {d["following_id"] for d in following_docs}

    return [
        {
            "id":           str(u["_id"]),
            "name":         u.get("name", ""),
            "username":     u.get("username", ""),
            "picture":      u.get("picture"),
            "is_following": str(u["_id"]) in following_set,
        }
        for u in users
    ]


# ─────────────────────────────────────────────────────────────────────────────
# POST /posts/{post_id}/comment_notify — Notify post author of new comment
# ─────────────────────────────────────────────────────────────────────────────

class _CommentNotifyBody(BaseModel):
    comment_text: str

@router.post("/{post_id}/comment_notify")
@limiter.limit("30/minute")
async def comment_notify(
    request:  Request,
    post_id:  str,
    body:     _CommentNotifyBody,
    user_id:  str = Depends(get_current_user_id),
    db             = Depends(get_db),
):
    """
    Called by the client when it posts a comment.
    Saves a notification record and sends FCM push to the post author.
    Does NOT store the comment itself (client handles local storage).
    """
    try:
        ObjectId(post_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid post ID")

    post = await db.posts.find_one({"_id": ObjectId(post_id)}, {"user_id": 1})
    if not post:
        return {"ok": True, "new_count": None}

    # Always increment comment count — even self-comments count
    result = await db.posts.find_one_and_update(
        {"_id": ObjectId(post_id)},
        {"$inc": {"comments_count": 1}},
        projection={"comments_count": 1},
        return_document=True,
    )
    new_count = result.get("comments_count", 0) if result else None

    owner_id = str(post.get("user_id", ""))
    if owner_id == user_id:
        return {"ok": True, "new_count": new_count}  # no self-notification, but count still updated

    commenter, owner = await asyncio.gather(
        db.users.find_one({"_id": ObjectId(user_id)}, {"name": 1, "username": 1}),
        db.users.find_one(
            {"_id": ObjectId(owner_id)},
            {"fcm_token": 1, "notification_settings": 1},
        ),
    )
    if not commenter or not owner:
        return {"ok": True}

    commenter_name     = commenter.get("name", "") or ""
    commenter_username = commenter.get("username", "") or ""
    fcm_token          = owner.get("fcm_token")
    _ns                = owner.get("notification_settings") or {}
    notif_master       = _ns.get("master", True)
    notif_comments     = _ns.get("comments", True)

    notif_id = str(ObjectId())
    now      = datetime.now(timezone.utc)

    await db.notifications.insert_one({
        "_id":           ObjectId(notif_id),
        "recipient_id":  owner_id,
        "type":          "comment",
        "from_user_id":  user_id,
        "from_username": commenter_username,
        "from_name":     commenter_name,
        "post_id":       post_id,
        "text":          f"commented: {body.comment_text[:80]}",
        "read":          False,
        "created_at":    now,
    })

    if fcm_token and is_fcm_ready() and notif_master and notif_comments:
        asyncio.create_task(
            send_comment_push(
                fcm_token=fcm_token,
                commenter_name=commenter_name,
                commenter_username=commenter_username,
                post_id=post_id,
                comment_text=body.comment_text,
                notif_id=notif_id,
            )
        )

    return {"ok": True, "new_count": new_count}


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

    post = await db.posts.find_one(
        {"_id": oid},
        {"user_id": 1, "public_id": 1, "media_type": 1, "section": 1},
    )
    logger.info(f"[DELETE_POST] post_id={post_id} caller={user_id} found={post is not None} stored_uid={post.get('user_id') if post else 'N/A'}")
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if post.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail=f"Not authorized: post owner={post.get('user_id')} caller={user_id}")

    section    = post.get("section")
    public_id  = post.get("public_id", "")
    media_type = post.get("media_type", "image")  # "image" | "video"

    # Delete from MongoDB first
    await db.posts.delete_one({"_id": oid})
    await asyncio.gather(
        db.post_likes.delete_many({"post_id": post_id}),
        db.notifications.delete_many({"post_id": post_id}),
    )

    # Delete from Cloudinary (queued with retry — don't block response)
    if public_id:
        await task_queue.enqueue(_delete_from_cloudinary, public_id, media_type)

    # Invalidate feed / shots cache
    await delete_cache_pattern(f"feed:u:{user_id}:*")
    if section:
        await delete_cache_pattern(f"shots:u:{user_id}:{section}:*")

    logger.info(f"[POST] deleted id={post_id} user={user_id} public_id={public_id}")
    return {"deleted": True}


async def _delete_from_cloudinary(public_id: str, media_type: str) -> None:
    """Fire-and-forget helper — deletes the asset from Cloudinary."""
    try:
        from app.services.media_service import get_media_provider
        provider = get_media_provider()
        resource_type = "video" if media_type == "video" else "image"
        ok = await provider.delete(public_id, resource_type=resource_type)
        if not ok:
            logger.warning(f"[CLOUDINARY] delete returned not-ok for {public_id}")
    except Exception as e:
        logger.error(f"[CLOUDINARY] delete task error public_id={public_id}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# COMMENTS — POST /posts/{post_id}/comments
#             GET  /posts/{post_id}/comments
#             DELETE /posts/comments/{comment_id}
#             POST   /posts/comments/{comment_id}/like
#             DELETE /posts/comments/{comment_id}/like
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_comment(doc: dict, current_user_id: str, liked_ids: set) -> dict:
    """Serialise a MongoDB comment document for the API response."""
    cid = str(doc["_id"])
    return {
        "id":           cid,
        "post_id":      doc.get("post_id", ""),
        "user_id":      doc.get("user_id", ""),
        "user_name":    doc.get("user_name", ""),
        "user_username":doc.get("user_username", ""),
        "user_picture": doc.get("user_picture"),
        "text":         doc.get("text", ""),
        "parent_id":    doc.get("parent_id"),
        "likes_count":  doc.get("likes_count", 0),
        "is_liked":     cid in liked_ids,
        "created_at":   doc["created_at"].isoformat() if "created_at" in doc else "",
    }


class _CommentBody(BaseModel):
    text: str
    parent_id: Optional[str] = None   # null → top-level; str → reply


@router.post("/{post_id}/comments")
@limiter.limit("30/minute")
async def create_comment(
    request:  Request,
    post_id:  str,
    body:     _CommentBody,
    user_id:  str = Depends(get_current_user_id),
    db             = Depends(get_db),
):
    """
    Post a new comment (or reply) on a post.
    - Stores in `comments` collection.
    - Increments `posts.comments_count` atomically.
    - Sends FCM + DB notification to the post owner (non-self).
    """
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Comment text cannot be empty.")
    if len(text) > 1000:
        raise HTTPException(status_code=400, detail="Comment too long (max 1000 chars).")

    try:
        post_oid = ObjectId(post_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid post ID.")

    post = await db.posts.find_one({"_id": post_oid}, {"user_id": 1})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")

    # Resolve parent: must belong to the same post, be top-level (no nested replies)
    parent_id: Optional[str] = None
    if body.parent_id:
        try:
            parent_oid = ObjectId(body.parent_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid parent_id.")
        parent_doc = await db.comments.find_one(
            {"_id": parent_oid, "post_id": post_id},
            {"parent_id": 1},
        )
        if not parent_doc:
            raise HTTPException(status_code=404, detail="Parent comment not found.")
        # Flatten: if parent is already a reply, attach to its own parent (1-level max)
        parent_id = parent_doc.get("parent_id") or body.parent_id

    # Fetch commenter info
    commenter = await db.users.find_one(
        {"_id": ObjectId(user_id)},
        {"name": 1, "username": 1, "picture": 1},
    )
    if not commenter:
        raise HTTPException(status_code=404, detail="User not found.")

    now = datetime.now(timezone.utc)
    comment_doc = {
        "post_id":      post_id,
        "user_id":      user_id,
        "user_name":    commenter.get("name", ""),
        "user_username":commenter.get("username", ""),
        "user_picture": commenter.get("picture"),
        "text":         text,
        "parent_id":    parent_id,
        "likes_count":  0,
        "created_at":   now,
    }
    result = await db.comments.insert_one(comment_doc)
    comment_doc["_id"] = result.inserted_id

    # Atomically increment post comments_count
    await db.posts.update_one({"_id": post_oid}, {"$inc": {"comments_count": 1}})

    # ── Notification (non-self, top-level comment only) ───────────────────────
    owner_id = str(post.get("user_id", ""))
    if owner_id != user_id and parent_id is None:
        owner = await db.users.find_one(
            {"_id": ObjectId(owner_id)},
            {"fcm_token": 1, "notification_settings": 1},
        )
        if owner:
            _ns            = owner.get("notification_settings") or {}
            notif_master   = _ns.get("master", True)
            notif_comments = _ns.get("comments", True)
            fcm_token      = owner.get("fcm_token")
            notif_id       = str(ObjectId())

            await db.notifications.insert_one({
                "_id":           ObjectId(notif_id),
                "recipient_id":  owner_id,
                "type":          "comment",
                "from_user_id":  user_id,
                "from_username": commenter.get("username", ""),
                "from_name":     commenter.get("name", ""),
                "post_id":       post_id,
                "text":          f"commented: {text[:80]}",
                "read":          False,
                "created_at":    now,
            })

            if fcm_token and is_fcm_ready() and notif_master and notif_comments:
                asyncio.create_task(
                    send_comment_push(
                        fcm_token=fcm_token,
                        commenter_name=commenter.get("name", ""),
                        commenter_username=commenter.get("username", ""),
                        post_id=post_id,
                        comment_text=text,
                        notif_id=notif_id,
                    )
                )

    return {
        "comment": _fmt_comment(comment_doc, user_id, set()),
        "new_count": None,  # not fetched here — caller already updates optimistically
    }


@router.get("/{post_id}/comments")
@limiter.limit("60/minute")
async def get_comments(
    request:  Request,
    post_id:  str,
    cursor:   Optional[str] = Query(None, description="Last seen comment _id for pagination"),
    limit:    int           = Query(20, ge=1, le=50),
    user_id:  str = Depends(get_current_user_id),
    db             = Depends(get_db),
):
    """
    Fetch top-level comments for a post (oldest-first, cursor-paginated).
    Each top-level comment includes its replies inline.
    """
    try:
        ObjectId(post_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid post ID.")

    # Cursor-based pagination — fetch comments AFTER the given cursor id
    query: dict = {"post_id": post_id, "parent_id": None}
    if cursor:
        try:
            query["_id"] = {"$gt": ObjectId(cursor)}
        except Exception:
            pass  # ignore bad cursor — return from beginning

    top_docs = await db.comments.find(query).sort("_id", 1).limit(limit).to_list(limit)

    if not top_docs:
        return {"comments": [], "next_cursor": None}

    top_ids = [str(d["_id"]) for d in top_docs]

    # Fetch all replies for these top-level comments in a single query
    reply_docs = await db.comments.find(
        {"post_id": post_id, "parent_id": {"$in": top_ids}}
    ).sort("_id", 1).to_list(200)

    # Fetch liked set for current user (both top-level and replies)
    all_ids = top_ids + [str(d["_id"]) for d in reply_docs]
    liked_docs = await db.comment_likes.find(
        {"comment_id": {"$in": all_ids}, "user_id": user_id},
        {"comment_id": 1},
    ).to_list(300)
    liked_ids = {d["comment_id"] for d in liked_docs}

    # Group replies by parent_id
    replies_by_parent: dict[str, list] = {}
    for r in reply_docs:
        pid = r.get("parent_id", "")
        replies_by_parent.setdefault(pid, []).append(_fmt_comment(r, user_id, liked_ids))

    # Build response
    comments_out = []
    for doc in top_docs:
        cid = str(doc["_id"])
        item = _fmt_comment(doc, user_id, liked_ids)
        item["replies"] = replies_by_parent.get(cid, [])
        comments_out.append(item)

    next_cursor = top_ids[-1] if len(top_docs) == limit else None

    return {"comments": comments_out, "next_cursor": next_cursor}


@router.delete("/comments/{comment_id}")
async def delete_comment(
    comment_id: str,
    user_id:    str = Depends(get_current_user_id),
    db               = Depends(get_db),
):
    """
    Delete a comment. Only the comment author or the post owner may delete.
    Decrements posts.comments_count (and replies' count) atomically.
    """
    try:
        coid = ObjectId(comment_id)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid comment ID.")

    comment = await db.comments.find_one({"_id": coid})
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found.")

    post_id = comment.get("post_id", "")
    post = await db.posts.find_one({"_id": ObjectId(post_id)}, {"user_id": 1}) if post_id else None
    post_owner = str(post.get("user_id", "")) if post else ""

    if comment.get("user_id") != user_id and post_owner != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this comment.")

    # Count replies (we'll decrement those too)
    reply_count = await db.comments.count_documents(
        {"post_id": post_id, "parent_id": comment_id}
    )

    await db.comments.delete_one({"_id": coid})
    await db.comments.delete_many({"post_id": post_id, "parent_id": comment_id})
    await db.comment_likes.delete_many({"comment_id": comment_id})

    total_decrement = 1 + reply_count
    if post_id:
        await db.posts.update_one(
            {"_id": ObjectId(post_id)},
            {"$inc": {"comments_count": -total_decrement}},
        )

    return {"deleted": True}


@router.post("/comments/{comment_id}/like")
@limiter.limit("60/minute")
async def like_comment(
    request:    Request,
    comment_id: str,
    user_id:    str = Depends(get_current_user_id),
    db               = Depends(get_db),
):
    try:
        coid = ObjectId(comment_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid comment ID.")

    comment = await db.comments.find_one({"_id": coid}, {"_id": 1})
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found.")

    from pymongo.errors import DuplicateKeyError
    try:
        await db.comment_likes.insert_one(
            {"comment_id": comment_id, "user_id": user_id, "created_at": datetime.now(timezone.utc)}
        )
        await db.comments.update_one({"_id": coid}, {"$inc": {"likes_count": 1}})
    except DuplicateKeyError:
        pass  # already liked — idempotent

    return {"liked": True}


@router.delete("/comments/{comment_id}/like")
@limiter.limit("60/minute")
async def unlike_comment(
    request:    Request,
    comment_id: str,
    user_id:    str = Depends(get_current_user_id),
    db               = Depends(get_db),
):
    try:
        coid = ObjectId(comment_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid comment ID.")

    result = await db.comment_likes.delete_one(
        {"comment_id": comment_id, "user_id": user_id}
    )
    if result.deleted_count:
        await db.comments.update_one({"_id": coid}, {"$inc": {"likes_count": -1}})

    return {"liked": False}
