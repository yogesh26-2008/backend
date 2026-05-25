"""
Media storage abstraction layer.

To swap Cloudinary for another provider (S3, R2, Bunny, etc.):
  1. Write a new class that extends MediaStorageProvider
  2. Set MEDIA_PROVIDER=your_new_provider in .env
  3. Add it to _build_provider() below — zero other changes needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import partial
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Shared result type — same shape regardless of provider
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UploadResult:
    url: str                          # CDN delivery URL (use this in the app)
    public_id: str                    # Provider-specific ID (for deletion/transforms)
    thumbnail_url: Optional[str] = None   # Auto-generated for videos
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None  # Seconds, for videos
    fmt: str = ""                     # e.g. "jpg", "mp4", "webp"
    bytes_size: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Abstract interface — NEVER import Cloudinary-specific things outside this file
# ─────────────────────────────────────────────────────────────────────────────

class MediaStorageProvider(ABC):

    @abstractmethod
    async def upload_image(
        self,
        file_bytes: bytes,
        folder: str,                  # "profiles" | "posts" | "stories" | "chats"
        public_id: Optional[str] = None,
    ) -> UploadResult: ...

    @abstractmethod
    async def upload_video(
        self,
        file_bytes: bytes,
        folder: str,
        public_id: Optional[str] = None,
    ) -> UploadResult: ...

    @abstractmethod
    async def delete(
        self,
        public_id: str,
        resource_type: str = "image",  # "image" | "video" | "raw"
    ) -> bool: ...

    @abstractmethod
    def optimized_image_url(
        self,
        public_id: str,
        width: int = 800,
        height: int = 800,
        crop: str = "fill",           # "fill" | "fit" | "thumb"
    ) -> str: ...

    @abstractmethod
    def thumbnail_url(self, public_id: str, width: int = 400) -> str: ...

    @abstractmethod
    def generate_upload_signature(
        self,
        folder: str,
        resource_type: str = "image",
        public_id: Optional[str] = None,
    ) -> dict:
        """
        Returns signed params so the Flutter client can upload DIRECTLY to the
        CDN without routing bytes through Railway.

        Security: API secret never leaves the backend.
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Cloudinary implementation
# ─────────────────────────────────────────────────────────────────────────────

class CloudinaryProvider(MediaStorageProvider):
    """
    Upload flow (recommended — signed direct upload):
      1. Flutter → POST /media/upload-signature  →  signed params
      2. Flutter → Cloudinary CDN (direct, no Railway proxy)
      3. Flutter → Backend with returned URL to save in MongoDB

    Fallback server upload:
      POST /media/upload-server  (for edge cases only)
    """

    _ROOT_FOLDER = "trandia"

    # Transformation shortcuts for low-data delivery
    _T_PROFILE   = "w_200,h_200,c_fill,f_auto,q_auto"
    _T_POST_IMG  = "w_1080,h_1080,c_limit,f_auto,q_auto"
    _T_THUMB     = "w_400,h_400,c_fill,f_auto,q_auto"
    _T_FEED_IMG  = "w_600,f_auto,q_auto:eco"  # even lower quality for feed preview

    def __init__(self, cloud_name: str, api_key: str, api_secret: str) -> None:
        import cloudinary
        import cloudinary.uploader
        import cloudinary.api

        self._cloud_name  = cloud_name
        self._api_key     = api_key
        self._api_secret  = api_secret

        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True,
        )
        # Keep module references so we don't re-import on every call
        self._uploader = cloudinary.uploader
        self._api      = cloudinary.api

    # ── helpers ──────────────────────────────────────────────────────────────

    def _full_folder(self, folder: str) -> str:
        return f"{self._ROOT_FOLDER}/{folder}"

    async def _run(self, func, *args, **kwargs):
        """Run Cloudinary's sync SDK in a thread so we don't block FastAPI."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    # ── uploads ──────────────────────────────────────────────────────────────

    async def upload_image(
        self,
        file_bytes: bytes,
        folder: str,
        public_id: Optional[str] = None,
    ) -> UploadResult:
        kw: dict = {
            "folder":        self._full_folder(folder),
            "resource_type": "image",
            "quality":       "auto",
            "fetch_format":  "auto",   # auto-delivers WebP/AVIF
            "eager": [
                {"width": 400, "height": 400, "crop": "fill",  "fetch_format": "auto", "quality": "auto"},
                {"width": 200, "height": 200, "crop": "fill",  "fetch_format": "auto", "quality": "auto:eco"},
            ],
            "eager_async": True,
        }
        if public_id:
            kw["public_id"] = public_id

        result = await self._run(self._uploader.upload, file_bytes, **kw)
        logger.info(f"[CLOUDINARY] image uploaded public_id={result['public_id']}")

        eager = result.get("eager") or []
        thumb = eager[0]["secure_url"] if eager else None

        return UploadResult(
            url=result["secure_url"],
            public_id=result["public_id"],
            thumbnail_url=thumb,
            width=result.get("width"),
            height=result.get("height"),
            fmt=result.get("format", ""),
            bytes_size=result.get("bytes", 0),
        )

    async def upload_video(
        self,
        file_bytes: bytes,
        folder: str,
        public_id: Optional[str] = None,
    ) -> UploadResult:
        kw: dict = {
            "folder":        self._full_folder(folder),
            "resource_type": "video",
            "quality":       "auto",
            "fetch_format":  "auto",
            # Generate a thumbnail at 1-second mark
            "eager": [
                {
                    "format": "jpg",
                    "width": 400, "height": 400, "crop": "fill",
                    "start_offset": "1",    # grab frame at 1s
                    "quality": "auto",
                }
            ],
            "eager_async": True,
        }
        if public_id:
            kw["public_id"] = public_id

        result = await self._run(self._uploader.upload, file_bytes, **kw)
        logger.info(f"[CLOUDINARY] video uploaded public_id={result['public_id']}")

        eager = result.get("eager") or []
        thumb = eager[0]["secure_url"] if eager else None

        return UploadResult(
            url=result["secure_url"],
            public_id=result["public_id"],
            thumbnail_url=thumb,
            width=result.get("width"),
            height=result.get("height"),
            duration=result.get("duration"),
            fmt=result.get("format", ""),
            bytes_size=result.get("bytes", 0),
        )

    # ── delete ───────────────────────────────────────────────────────────────

    async def delete(self, public_id: str, resource_type: str = "image") -> bool:
        try:
            result = await self._run(
                self._uploader.destroy,
                public_id,
                resource_type=resource_type,
            )
            ok = result.get("result") == "ok"
            logger.info(f"[CLOUDINARY] delete public_id={public_id} result={result.get('result')}")
            return ok
        except Exception as e:
            logger.error(f"[CLOUDINARY] delete error: {e}")
            return False

    # ── URL helpers (zero API calls — pure string transforms) ─────────────────

    def _base_url(self, resource_type: str, public_id: str, transforms: str) -> str:
        return (
            f"https://res.cloudinary.com/{self._cloud_name}"
            f"/{resource_type}/upload/{transforms}/{public_id}"
        )

    def optimized_image_url(
        self,
        public_id: str,
        width: int = 800,
        height: int = 800,
        crop: str = "fill",
    ) -> str:
        t = f"w_{width},h_{height},c_{crop},f_auto,q_auto"
        return self._base_url("image", public_id, t)

    def thumbnail_url(self, public_id: str, width: int = 400) -> str:
        t = f"w_{width},h_{width},c_fill,f_auto,q_auto:eco"
        return self._base_url("image", public_id, t)

    # ── signed upload (recommended for Flutter) ───────────────────────────────

    def generate_upload_signature(
        self,
        folder: str,
        resource_type: str = "image",
        public_id: Optional[str] = None,
    ) -> dict:
        """
        Flutter uses these params to upload directly to Cloudinary.
        API secret is never sent to the client.
        """
        timestamp = int(_time.time())
        full_folder = self._full_folder(folder)

        params: dict = {
            "folder":    full_folder,
            "timestamp": timestamp,
        }
        if public_id:
            params["public_id"] = public_id

        # Cloudinary signature: SHA-256(sorted_params + api_secret)
        param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        param_str += self._api_secret
        signature = hashlib.sha256(param_str.encode("utf-8")).hexdigest()

        resource = resource_type  # "image" or "video"
        upload_url = (
            f"https://api.cloudinary.com/v1_1/{self._cloud_name}/{resource}/upload"
        )

        return {
            "cloud_name":  self._cloud_name,
            "api_key":     self._api_key,
            "timestamp":   timestamp,
            "signature":   signature,
            "folder":      full_folder,
            "upload_url":  upload_url,
            "resource_type": resource_type,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Factory — the only place that knows about concrete providers
# ─────────────────────────────────────────────────────────────────────────────

def _build_provider(provider_name: str, settings) -> MediaStorageProvider:
    if provider_name == "cloudinary":
        return CloudinaryProvider(
            cloud_name=settings.cloudinary_cloud_name,
            api_key=settings.cloudinary_api_key,
            api_secret=settings.cloudinary_api_secret,
        )
    # Future providers: "s3", "r2", "bunny", etc.
    # elif provider_name == "s3":
    #     return S3Provider(...)
    raise ValueError(
        f"Unknown MEDIA_PROVIDER '{provider_name}'. "
        "Supported: cloudinary  (add more in media_service._build_provider)"
    )


# Module-level singleton — initialized once at app startup
_provider: Optional[MediaStorageProvider] = None


def init_media_provider(settings) -> None:
    global _provider
    _provider = _build_provider(settings.media_provider, settings)
    logger.info(f"[MEDIA] provider='{settings.media_provider}' initialized")


def get_media_provider() -> MediaStorageProvider:
    if _provider is None:
        raise RuntimeError("Media provider not initialized. Call init_media_provider() at startup.")
    return _provider
