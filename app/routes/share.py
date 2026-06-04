import html as html_lib
import logging
import secrets
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.database import get_db
from app.utils.jwt_handler import get_current_user_id

share_router = APIRouter()    # mounted at /share
preview_router = APIRouter()  # mounted at root (/)

logger = logging.getLogger(__name__)

_BASE_URL = "https://trandia.in"
_PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=com.trandia.trandia"


def _generate_token() -> str:
    # 8 URL-safe characters
    return secrets.token_urlsafe(8)[:8]


# ─────────────────────────────────────────────────────────────────────────────
# HTML template — uses [[key]] placeholders to avoid f-string brace conflicts
# ─────────────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>[[og_title]]</title>
<meta property="og:type" content="video.other">
<meta property="og:title" content="[[og_title]]">
<meta property="og:description" content="[[og_description]]">
<meta property="og:image" content="[[og_image]]">
<meta property="og:url" content="[[og_url]]">
<meta property="og:site_name" content="Trandia">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="[[og_title]]">
<meta name="twitter:description" content="[[og_description]]">
<meta name="twitter:image" content="[[og_image]]">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#fff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center}
.vwrap{position:relative;width:100%;max-width:480px;background:#000;flex-shrink:0}
video{width:100%;display:block;max-height:68vh;object-fit:contain;background:#000}
.overlay{position:absolute;inset:0;z-index:2;cursor:pointer}
.pbtn{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:3;background:rgba(0,0,0,.55);border:none;border-radius:50%;width:64px;height:64px;cursor:pointer;display:flex;align-items:center;justify-content:center;-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);transition:opacity .2s}
.pbtn svg{fill:#fff;width:26px;height:26px}
.pi{margin-left:4px}
.info{width:100%;max-width:480px;padding:18px 16px;background:#111;border-top:1px solid #1e1e1e}
.creator{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.av{width:44px;height:44px;border-radius:50%;background:linear-gradient(135deg,#00c853,#1de9b6);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:19px;color:#000;flex-shrink:0}
.ci{flex:1;min-width:0}
.cn{font-weight:700;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cu{color:#777;font-size:13px;margin-top:2px}
.caption{font-size:14px;line-height:1.6;color:#ccc;margin-bottom:14px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.brand{font-size:12px;color:#555;letter-spacing:.4px}
.brand b{color:#00c853}
.openbtn{display:block;width:calc(100% - 32px);max-width:448px;margin:14px 16px 0;padding:14px;background:linear-gradient(135deg,#00c853,#00e676);color:#000;font-weight:800;font-size:15px;border:none;border-radius:14px;cursor:pointer;text-align:center}
.sb{position:fixed;bottom:0;left:0;right:0;background:#0f0f0f;border-top:1px solid #1e1e1e;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;z-index:100;gap:12px}
.sb p{font-size:12px;color:#777;line-height:1.5}
.sb p strong{display:block;color:#fff;font-size:14px;font-weight:700}
.dlbtn{flex-shrink:0;background:#00c853;color:#000;font-weight:800;font-size:13px;padding:10px 18px;border-radius:10px;text-decoration:none;white-space:nowrap}
.sp{height:80px}
</style>
</head>
<body>

<!-- ZONE 1: Video Player -->
<div class="vwrap">
  <video id="vid" src="[[video_url]]" poster="[[thumbnail_url]]"
         playsinline autoplay muted loop preload="metadata"></video>
  <!-- Transparent overlay: any tap on the video area goes to Play Store (z-index 2) -->
  <div class="overlay" onclick="goStore()"></div>
  <!-- Play/pause button sits above the overlay (z-index 3) so it still works -->
  <button class="pbtn" onclick="togglePlay(event)" aria-label="Play/Pause">
    <svg id="pi" class="pi" viewBox="0 0 24 24"><polygon points="5,3 19,12 5,21"/></svg>
    <svg id="pa" viewBox="0 0 24 24" style="display:none"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
  </button>
</div>

<!-- ZONE 2: Info Card -->
<div class="info">
  <div class="creator">
    <div class="av">[[avatar_letter]]</div>
    <div class="ci">
      <div class="cn">[[creator_name]]</div>
      <div class="cu">@[[creator_username]]</div>
    </div>
  </div>
  [[caption_block]]
  <p class="brand"><b>Trandia</b> — Scroll Smart. Grow Fast.</p>
</div>

<!-- Open in App button -->
<button class="openbtn" onclick="openInApp()">Open in Trandia App</button>

<div class="sp"></div>

<!-- ZONE 3: Sticky bottom banner -->
<div class="sb">
  <p><strong>Trandia</strong>Free &middot; Android</p>
  <a href="[[play_store_url]]" class="dlbtn" target="_blank" rel="noopener">Download Free</a>
</div>

<script>
var vid=document.getElementById('vid');
var pi=document.getElementById('pi');
var pa=document.getElementById('pa');
function upd(){pi.style.display=vid.paused?'':'none';pa.style.display=vid.paused?'none':'';}
vid.addEventListener('play',upd);
vid.addEventListener('pause',upd);
vid.addEventListener('canplay',function(){vid.play().catch(function(){});});
function togglePlay(e){e.stopPropagation();if(vid.paused){vid.play();}else{vid.pause();}}
function goStore(){window.location.href='[[play_store_url]]';}
function openInApp(){
  window.location.href='[[deep_link]]';
  setTimeout(function(){window.location.href='[[play_store_url]]';},2000);
}
document.addEventListener('touchstart',function(){vid.play().catch(function(){});},{once:true});
upd();
</script>
</body>
</html>"""


def _render_html(**kwargs) -> str:
    out = _HTML
    for key, val in kwargs.items():
        out = out.replace(f"[[{key}]]", str(val))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# POST /share/create — Generate a short share link
# ─────────────────────────────────────────────────────────────────────────────

class CreateShareLinkBody(BaseModel):
    videoId: str
    creatorId: str
    videoType: str = "shot"   # "shot" | "ttube"


@share_router.post("/create")
async def create_share_link(
    body: CreateShareLinkBody,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    try:
        ObjectId(body.videoId)
    except (InvalidId, Exception):
        raise HTTPException(status_code=400, detail="Invalid videoId")

    # Generate a unique 8-character token (up to 10 attempts)
    for _ in range(10):
        token = _generate_token()
        if not await db.share_links.find_one({"token": token}):
            break
    else:
        raise HTTPException(status_code=500, detail="Token generation failed, please retry")

    await db.share_links.insert_one({
        "token": token,
        "videoId": body.videoId,
        "creatorId": body.creatorId,
        "videoType": body.videoType,
        "clicks": 0,
        "createdAt": datetime.now(timezone.utc),
    })
    logger.info(f"[SHARE] Created link token={token} video={body.videoId} by user={user_id}")

    return {"url": f"{_BASE_URL}/v/{token}", "token": token}


# ─────────────────────────────────────────────────────────────────────────────
# GET /v/{token} — Serve web preview page + increment click count
# ─────────────────────────────────────────────────────────────────────────────

@preview_router.get("/v/{token}", response_class=HTMLResponse, include_in_schema=False)
async def video_preview(token: str, db=Depends(get_db)):
    link = await db.share_links.find_one({"token": token})
    if not link:
        raise HTTPException(status_code=404, detail="Link not found")

    # Fire-and-forget click increment
    await db.share_links.update_one({"token": token}, {"$inc": {"clicks": 1}})

    video_id = link.get("videoId", "")
    try:
        post = await db.posts.find_one({"_id": ObjectId(video_id)})
    except Exception:
        post = None

    if not post:
        raise HTTPException(status_code=404, detail="Video not found")

    creator_name     = (post.get("user_name") or "Trandia Creator").strip()
    creator_username = (post.get("user_username") or "trandia").strip()
    caption          = (post.get("caption") or "").strip()
    media_url        = post.get("media_url") or ""
    thumbnail_url    = post.get("thumbnail_url") or media_url

    og_title       = html_lib.escape(f"{creator_name} on Trandia")
    og_description = html_lib.escape(
        caption[:200] if caption else "Watch this on Trandia — Scroll Smart. Grow Fast."
    )
    caption_block = (
        f'<p class="caption">{html_lib.escape(caption)}</p>' if caption else ""
    )

    # URL values flow into HTML attributes too. media_url is validated at
    # post-creation, but thumbnail_url is client-supplied — an un-escaped value
    # could break out of the attribute (stored XSS). Valid Cloudinary URLs are
    # unaffected by escaping (& becomes &amp;, which browsers decode back).
    safe_media_url = html_lib.escape(media_url, quote=True)
    safe_thumbnail_url = html_lib.escape(thumbnail_url, quote=True)
    safe_og_url = html_lib.escape(f"{_BASE_URL}/v/{token}", quote=True)

    return HTMLResponse(content=_render_html(
        og_title=og_title,
        og_description=og_description,
        og_image=safe_thumbnail_url,
        og_url=safe_og_url,
        video_url=safe_media_url,
        thumbnail_url=safe_thumbnail_url,
        creator_name=html_lib.escape(creator_name),
        creator_username=html_lib.escape(creator_username),
        avatar_letter=html_lib.escape(creator_name[0].upper()),
        caption_block=caption_block,
        deep_link=f"trandia://video/{video_id}",
        play_store_url=_PLAY_STORE_URL,
    ))
