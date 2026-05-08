from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from app.config import settings
from app.models.user import UserCreate, UserLogin, UserResponse, AuthResponse
from app.utils.jwt_handler import create_access_token
from app.utils.password import hash_password, verify_password
from app.services.notification_service import send_welcome_notification


def _build_auth_response(user_doc: dict, message: str) -> dict:
    uid = str(user_doc["_id"])
    token = create_access_token(uid, user_doc["email"])
    user = UserResponse(
        id=uid,
        name=user_doc["name"],
        username=user_doc["username"],
        email=user_doc["email"],
        picture=user_doc.get("picture"),
        is_google_user=user_doc.get("is_google_user", False),
        created_at=user_doc["created_at"],
    )
    return AuthResponse(access_token=token, user=user, message=message)


async def signup_with_email(data: UserCreate, db: AsyncIOMotorDatabase) -> AuthResponse:
    if await db.users.find_one({"email": data.email}):
        raise HTTPException(status_code=400, detail="Email is already registered")
    if await db.users.find_one({"username": data.username}):
        raise HTTPException(status_code=400, detail="Username is already taken")

    doc = {
        "name": data.name,
        "username": data.username,
        "email": data.email,
        "password_hash": hash_password(data.password),
        "is_google_user": False,
        "google_id": None,
        "picture": None,
        "fcm_token": data.fcm_token,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "last_login": datetime.now(timezone.utc),
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id

    await send_welcome_notification(data.fcm_token, data.name, is_signup=True)
    return _build_auth_response(doc, "Account created successfully. Welcome to Trandia!")


async def login_with_email(data: UserLogin, db: AsyncIOMotorDatabase) -> AuthResponse:
    user = await db.users.find_one({"email": data.email})
    if not user or not user.get("password_hash"):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not verify_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_login": datetime.now(timezone.utc), "fcm_token": data.fcm_token}},
    )
    if data.fcm_token:
        user["fcm_token"] = data.fcm_token

    await send_welcome_notification(data.fcm_token, user["name"], is_signup=False)
    return _build_auth_response(user, "Welcome back to Trandia!")


async def auth_with_google_userinfo(
    userinfo: dict, fcm_token: str | None, db: AsyncIOMotorDatabase
) -> AuthResponse:
    email = userinfo["email"]
    google_id = userinfo["sub"]
    name = userinfo.get("name", email.split("@")[0])
    picture = userinfo.get("picture")

    existing = await db.users.find_one({"email": email})
    if existing:
        await db.users.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    "last_login": datetime.now(timezone.utc),
                    "google_id": google_id,
                    "picture": picture,
                    "fcm_token": fcm_token,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        existing.update({"picture": picture, "fcm_token": fcm_token})
        await send_welcome_notification(fcm_token, existing["name"], is_signup=False)
        return _build_auth_response(existing, "Welcome back to Trandia!")

    base_username = email.split("@")[0].lower().replace(".", "")
    username = base_username
    counter = 1
    while await db.users.find_one({"username": username}):
        username = f"{base_username}{counter}"
        counter += 1

    doc = {
        "name": name,
        "username": username,
        "email": email,
        "password_hash": None,
        "is_google_user": True,
        "google_id": google_id,
        "picture": picture,
        "fcm_token": fcm_token,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "last_login": datetime.now(timezone.utc),
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id

    await send_welcome_notification(fcm_token, name, is_signup=True)
    return _build_auth_response(doc, "Account created with Google. Welcome to Trandia!")


async def auth_with_google_id_token(
    token_str: str, fcm_token: str | None, db: AsyncIOMotorDatabase
) -> AuthResponse:
    idinfo = None
    for audience in [settings.google_android_client_id, settings.google_client_id]:
        try:
            idinfo = id_token.verify_oauth2_token(
                token_str, google_requests.Request(), audience
            )
            break
        except ValueError:
            continue

    if not idinfo:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    return await auth_with_google_userinfo(idinfo, fcm_token, db)
