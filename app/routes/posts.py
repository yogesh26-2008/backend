from fastapi import APIRouter, Depends
from typing import List, Any
from app.database import get_db
from app.utils.jwt_handler import get_current_user_id

router = APIRouter()


@router.get("/")
async def get_posts(
    skip: int = 0,
    limit: int = 20,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
) -> List[Any]:
    limit = min(limit, 100)  # Max 100 per request
    
    # We serialize the ObjectId properly or convert directly to dict
    cursor = db.posts.find({}).sort("created_at", -1).skip(skip).limit(limit)
    
    posts = await cursor.to_list(length=limit)
    
    # Convert ObjectIds to string for JSON serialization
    for post in posts:
        post["_id"] = str(post["_id"])
        if "user_id" in post:
            post["user_id"] = str(post["user_id"])
            
    return posts
