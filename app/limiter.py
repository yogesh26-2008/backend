from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

# Rate-limit state lives in Redis when REDIS_URL is configured, so limits are
# shared across every worker/instance instead of being counted per-process.
# (With 4 uvicorn workers, in-memory limits would effectively be 4x too loose.)
#
# swallow_errors=True → if the storage backend hiccups, the limiter fails OPEN
# (allows the request) instead of erroring the API. A Redis blip must never turn
# into 500s for users.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.redis_url or "memory://",
    swallow_errors=True,
)
