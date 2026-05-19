from fastapi import APIRouter, Depends, HTTPException
from app.database import get_db
from app.models.user import UserResponse, FCMTokenUpdate
from app.utils.jwt_handler import get_current_user_id
from bson import ObjectId
from datetime import datetime, timezone

from typing import List
import re

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def get_me(user_id: str = Depends(get_current_user_id), db=Depends(get_db)):
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(
        id=str(user["_id"]),
        name=user["name"],
        username=user["username"],
        email=user["email"],
        picture=user.get("picture"),
        is_google_user=user.get("is_google_user", False),
        created_at=user["created_at"],
    )


@router.put("/me/fcm-token")
async def update_fcm_token(
    data: FCMTokenUpdate,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db),
):
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"fcm_token": data.fcm_token, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"detail": "FCM token updated"}

@router.get("/search", response_model=List[UserResponse])
async def search_users(
    q: str = "",
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_db)
):
    if not q:
        return []
        
    regex = re.compile(f".*{q}.*", re.IGNORECASE)
    
    # Exclude current user from search results
    cursor = db.users.find({
        "$and": [
            {"_id": {"$ne": ObjectId(user_id)}},
            {"$or": [
                {"username": {"$regex": regex}},
                {"name": {"$regex": regex}}
            ]}
        ]
    }).limit(20)
    
    users = await cursor.to_list(length=20)
    
    return [
        UserResponse(
            id=str(u["_id"]),
            name=u["name"],
            username=u["username"],
            email=u["email"],
            picture=u.get("picture"),
            is_google_user=u.get("is_google_user", False),
            created_at=u["created_at"]
        ) for u in users
    ]
