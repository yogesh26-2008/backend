import logging

logger = logging.getLogger(__name__)
import json
from typing import Optional
import redis.asyncio as redis

# Redis client placeholder - connection string should ideally be in config.py
# For now, we connect to default localhost redis
_redis_client: Optional[redis.Redis] = None


async def init_redis(redis_url: str = "redis://localhost:6379"):
    """Initialize Redis connection."""
    global _redis_client
    try:
        _redis_client = await redis.from_url(redis_url, decode_responses=True)
        await _redis_client.ping()
        logger.info("[CACHE] Connected to Redis")
    except Exception as e:
        logger.error(f"[CACHE] Could not connect to Redis: {e}")
        _redis_client = None


async def close_redis():
    """Close Redis connection."""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        logger.info("[CACHE] Redis connection closed")


async def get_cache(key: str) -> Optional[dict]:
    """Get item from cache."""
    if not _redis_client:
        return None
    data = await _redis_client.get(key)
    if data:
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None
    return None


async def set_cache(key: str, value: dict, expire_seconds: int = 300):
    """Set item in cache."""
    if not _redis_client:
        return
    await _redis_client.set(key, json.dumps(value), ex=expire_seconds)


async def delete_cache(key: str) -> None:
    """Delete a single cache entry."""
    if not _redis_client:
        return
    await _redis_client.delete(key)


async def delete_cache_pattern(pattern: str) -> None:
    """Delete all cache keys matching a glob pattern, e.g. 'feed:u:abc123:*'."""
    if not _redis_client:
        return
    keys = await _redis_client.keys(pattern)
    if keys:
        await _redis_client.delete(*keys)
