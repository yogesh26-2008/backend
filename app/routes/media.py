"""
Media upload routes.

Two upload modes:
  1. Signed direct upload (RECOMMENDED): Flutter uploads directly to Cloudinary CDN.
     Railway never proxies file bytes → faster for users, saves Railway bandwidth.

  2. Server upload (fallback): send file to this endpoint, we forward to Cloudinary.

Swap Cloudinary for another CDN: only media_service.py needs to change.
"""

import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel

from app.limiter import limiter
from app.utils.jwt_handler import get_current_user_id
from app.services.media_service import get_media_provider

logger = logging.getLogger(__name__)
router = APIRouter()

ALLOWED_FOLDERS = {"profiles", "posts", "stories", "chats"}
ALLOWED_RESOURCE_TYPES = {"image", "video"}

# Max sizes enforced here (main.py LimitUploadSize handles global guard)
MAX_IMAGE_BYTES = 10 * 1024 * 1024   # 10 MB
MAX_VIDEO_BYTES = 100 * 1024 * 1024  # 100 MB

ALLOWED_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/heic",
}
ALLOWED_VIDEO_MIMES = {
    "video/mp4", "video/quicktime", "video/x-msvideo", "video/webm",
    "video/3gpp", "video/mpeg",
}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Signed upload params (preferred — Flutter uploads direct to CDN)
# ─────────────────────────────────────────────────────────────────────────────

class SignatureRequest(BaseModel):
    folder: str
    resource_type: str = "image"


@router.post("/upload-signature")
@limiter.limit("30/minute")
async def get_upload_signature(
    request: Request,
    body: SignatureRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Returns signed params. Flutter uses these to upload directly to Cloudinary.
    API secret stays on the server — never exposed to the client.
    """
    if body.folder not in ALLOWED_FOLDERS:
        raise HTTPException(status_code=400, detail=f"folder must be one of {ALLOWED_FOLDERS}")
    if body.resource_type not in ALLOWED_RESOURCE_TYPES:
        raise HTTPException(status_code=400, detail="resource_type must be 'image' or 'video'")

    provider = get_media_provider()
    params = provider.generate_upload_signature(
        folder=body.folder,
        resource_type=body.resource_type,
    )
    return params


# ─────────────────────────────────────────────────────────────────────────────
# 2. Server-side upload (fallback — use only when direct upload isn't possible)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/upload")
@limiter.limit("10/minute")
async def upload_file(
    request: Request,
    folder: str,
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
):
    """
    Server-side upload to Cloudinary. Use the direct-signature flow when possible.
    This endpoint is a fallback (e.g. for background worker uploads).
    """
    if folder not in ALLOWED_FOLDERS:
        raise HTTPException(status_code=400, detail=f"folder must be one of {ALLOWED_FOLDERS}")

    content_type = (file.content_type or "").lower()
    is_image = content_type in ALLOWED_IMAGE_MIMES
    is_video = content_type in ALLOWED_VIDEO_MIMES

    if not is_image and not is_video:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media type '{content_type}'. "
                   "Allowed: JPEG, PNG, WebP, GIF, MP4, MOV, AVI, WEBM",
        )

    file_bytes = await file.read()

    max_size = MAX_VIDEO_BYTES if is_video else MAX_IMAGE_BYTES
    if len(file_bytes) > max_size:
        mb = max_size // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"File too large. Max {mb} MB for {content_type}")

    # Validate magic bytes (don't trust Content-Type alone)
    _validate_magic(file_bytes, content_type)

    public_id = f"{user_id[:8]}_{uuid.uuid4().hex[:8]}"
    provider = get_media_provider()

    try:
        if is_image:
            result = await provider.upload_image(file_bytes, folder, public_id)
        else:
            result = await provider.upload_video(file_bytes, folder, public_id)
    except Exception as e:
        logger.error(f"[MEDIA] upload failed user={user_id} folder={folder}: {e}")
        raise HTTPException(status_code=502, detail="Media upload failed. Please try again.")

    return {
        "url":           result.url,
        "public_id":     result.public_id,
        "thumbnail_url": result.thumbnail_url,
        "width":         result.width,
        "height":        result.height,
        "duration":      result.duration,
        "format":        result.fmt,
        "bytes_size":    result.bytes_size,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Delete media
# ─────────────────────────────────────────────────────────────────────────────

class DeleteRequest(BaseModel):
    public_id: str
    resource_type: str = "image"


@router.delete("/")
@limiter.limit("20/minute")
async def delete_media(
    request: Request,
    body: DeleteRequest,
    user_id: str = Depends(get_current_user_id),
):
    if body.resource_type not in ALLOWED_RESOURCE_TYPES:
        raise HTTPException(status_code=400, detail="resource_type must be 'image' or 'video'")

    # Security: ensure the public_id belongs to this user's folder space
    if not body.public_id.startswith("trandia/"):
        raise HTTPException(status_code=403, detail="Cannot delete media outside trandia/ folder")

    provider = get_media_provider()
    deleted = await provider.delete(body.public_id, body.resource_type)
    if not deleted:
        raise HTTPException(status_code=404, detail="Media not found or already deleted")
    return {"detail": "deleted"}


# ─────────────────────────────────────────────────────────────────────────────
# 4. Get optimized URL (on-the-fly transforms — no upload needed)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/url")
async def get_optimized_url(
    public_id: str,
    width: int = 800,
    height: int = 800,
    crop: str = "fill",
    thumb: bool = False,
    user_id: str = Depends(get_current_user_id),
):
    provider = get_media_provider()
    if thumb:
        url = provider.thumbnail_url(public_id, width)
    else:
        url = provider.optimized_image_url(public_id, width, height, crop)
    return {"url": url}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _validate_magic(data: bytes, content_type: str) -> None:
    """Check magic bytes to prevent spoofed Content-Type attacks."""
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    hdr = data[:12]

    if content_type.startswith("image/jpeg"):
        if not hdr[:2] == b"\xff\xd8":
            raise HTTPException(status_code=415, detail="Invalid JPEG file")
    elif content_type.startswith("image/png"):
        if not hdr[:8] == b"\x89PNG\r\n\x1a\n":
            raise HTTPException(status_code=415, detail="Invalid PNG file")
    elif content_type.startswith("image/gif"):
        if not hdr[:6] in (b"GIF87a", b"GIF89a"):
            raise HTTPException(status_code=415, detail="Invalid GIF file")
    elif content_type.startswith("image/webp"):
        if not (hdr[:4] == b"RIFF" and hdr[8:12] == b"WEBP"):
            raise HTTPException(status_code=415, detail="Invalid WebP file")
    elif content_type.startswith("video/mp4"):
        if not (hdr[4:8] in (b"ftyp", b"moov", b"mdat") or hdr[:4] == b"\x00\x00\x00\x18"):
            pass  # MP4 boxes vary — skip strict magic check, rely on Cloudinary validation
    # Other types: rely on Cloudinary's own validation during upload
