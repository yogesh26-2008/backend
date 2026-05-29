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


def optimize_image(url: str, width: int = 480) -> str:
    """
    Return a Cloudinary image URL that serves:
    - auto format   (WebP for Android/Chrome, AVIF where supported)
    - eco quality   (aggressive compression — ~40% smaller than 'good')
    - max width     (c_limit preserves aspect ratio, never upscales)

    No-ops if the URL is not a Cloudinary URL or already has transforms.
    """
    if not url or "res.cloudinary.com" not in url or "/upload/" not in url:
        return url
    if "f_auto" in url or "q_auto" in url or "w_" in url:
        return url  # already optimised
    return _inject(url, f"f_auto,q_auto:eco,w_{width},c_limit")


def optimize_thumbnail(url: str) -> str:
    """Smaller transform for thumbnail previews (300 px wide, eco quality)."""
    return optimize_image(url, width=300)


def optimize_video(url: str) -> str:
    """
    Return a Cloudinary video URL with:
    - eco quality   (lower bitrate, smaller files)
    - auto codec    (H.264/H.265 based on device support)
    - max 720p      (no 1080p in feed — huge bandwidth saving)
    - 1.2 Mbps cap  (smooth on 3G/4G without buffering)
    """
    if not url or "res.cloudinary.com" not in url or "/upload/" not in url:
        return url
    if "q_auto" in url or "vc_auto" in url:
        return url
    return _inject(url, "q_auto:eco,vc_auto,w_720,c_limit,br_1200k")
