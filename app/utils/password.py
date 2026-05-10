import asyncio
import bcrypt


async def hash_password(plain: str) -> str:
    """Async bcrypt hash — runs in thread pool so event loop stays free."""
    hashed = await asyncio.to_thread(
        lambda: bcrypt.hashpw(plain.encode(), bcrypt.gensalt())
    )
    return hashed.decode()


async def verify_password(plain: str, hashed: str) -> bool:
    """Async bcrypt verify — runs in thread pool so event loop stays free."""
    return await asyncio.to_thread(
        lambda: bcrypt.checkpw(plain.encode(), hashed.encode())
    )
