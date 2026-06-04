"""
create_indexes.py — One-shot manual trigger for MongoDB index setup.

⚠️ SINGLE SOURCE OF TRUTH: app/database.py :: _create_indexes()
The running app already creates every index automatically on startup
(see app/database.py). This script just runs that SAME logic on demand — it
does NOT define its own index list, so the two can never drift or conflict.

Usage:
  cd backend
  python create_indexes.py
"""

import asyncio

from app.database import connect_db, close_db


async def main() -> None:
    # connect_db() pings MongoDB and then runs _create_indexes() — the exact
    # same index set the production app uses. No duplication, no conflicts.
    await connect_db()
    await close_db()
    print("✅ All indexes verified/created via app.database (single source of truth).")


if __name__ == "__main__":
    asyncio.run(main())
