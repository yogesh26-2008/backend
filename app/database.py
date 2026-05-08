import certifi
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING
from app.config import settings

_client: AsyncIOMotorClient = None
_db: AsyncIOMotorDatabase = None


async def connect_db():
    global _client, _db
    _client = AsyncIOMotorClient(
        settings.mongodb_url,
        serverSelectionTimeoutMS=30000,
        connectTimeoutMS=30000,
        socketTimeoutMS=30000,
        tlsCAFile=certifi.where(),
    )
    _db = _client[settings.mongodb_db]
    try:
        await _client.admin.command("ping")
        print(f"[DB] ✅ Connected to MongoDB — database: {settings.mongodb_db}")
    except Exception as e:
        print(f"[DB] ❌ Could not connect — {e}")
        return
    try:
        await _db.users.create_index([("email", ASCENDING)], unique=True, background=True)
        await _db.users.create_index([("username", ASCENDING)], unique=True, background=True)
        print("[DB] Indexes verified.")
    except Exception:
        pass


async def close_db():
    global _client
    if _client:
        _client.close()
        print("[DB] MongoDB connection closed")


def get_db() -> AsyncIOMotorDatabase:
    return _db
