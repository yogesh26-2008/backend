"""
Cloudinary URL transformation helpers.

Injects transformation parameters into existing Cloudinary URLs so clients
receive smaller, properly-encoded files — reducing bandwidth, latency, and
battery drain without changing the upload pipeline.

Format:  https://res.cloudinary.com/{cloud}/image/upload/{transforms}/{rest}
We inject transforms right after /upload/ on the first match only.
"""


def _inject(url: str, transform: str) -> str:
    """Insert `transform` string immediately after '/upload/' in a Cloudinary URL."""
    return url.replace("/upload/", f"/upload/{transform}/", 1)


def optimize_image(url: str, width: int = 600) -> str:
    """
    Return a Cloudinary image URL that serves:
    - auto format   (WebP for Android, AVIF where supported)
    - auto quality  (Cloudinary picks best quality for the size)
    - max width     (c_limit preserves aspect ratio, never upscales)

    No-ops if the URL is not a Cloudinary URL or already has transforms.
    """
    if not url or "res.cloudinary.com" not in url or "/upload/" not in url:
        return url
    if "f_auto" in url or "q_auto" in url or "w_" in url:
        return url  # already optimised
    return _inject(url, f"f_auto,q_auto:good,w_{width},c_limit")


def optimize_thumbnail(url: str) -> str:
    """Smaller transform for thumbnail previews (400 px wide)."""
    return optimize_image(url, width=400)


def optimize_video(url: str) -> str:
    """
    Return a Cloudinary video URL with auto quality + auto codec.
    Cloudinary picks H.264/H.265 and drops bitrate for slow connections.
    """
    if not url or "res.cloudinary.com" not in url or "/upload/" not in url:
        return url
    if "q_auto" in url or "vc_auto" in url:
        return url
    return _inject(url, "q_auto,vc_auto")
