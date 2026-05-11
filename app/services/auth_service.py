import asyncio
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from firebase_admin import auth as firebase_admin_auth
from typing import Optional

from app.config import settings
from app.models.user import UserCreate, UserLogin, UserResponse, AuthResponse, FirebaseSignupRequest
from app.utils.jwt_handler import create_access_token
from app.utils.password import hash_password, verify_password
from app.services.notification_service import schedule_welcome_notification

_DUMMY_HASH = "$2b$12$invalidhashfortimingequalisation.AAAAAAAAAAAAAAAAAAAAAA"


def _build_auth_response(user_doc: dict, message: str) -> AuthResponse:
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


def _require_db(db: AsyncIOMotorDatabase):
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Database not available. Please try again in a moment.",
        )


async def _verify_google_id_token(token_str: str) -> dict | None:
    for audience in [settings.google_android_client_id, settings.google_client_id]:
        try:
            result = await asyncio.to_thread(
                id_token.verify_oauth2_token,
                token_str,
                google_requests.Request(),
                audience,
            )
            return result
        except ValueError:
            continue
    return None


# ── Firebase Email Verification Signup ───────────────────────────────────────

async def signup_with_firebase_verified_email(
    data: FirebaseSignupRequest, db: AsyncIOMotorDatabase
) -> AuthResponse:
    """
    Called after Firebase email verification is complete.
    Verifies the Firebase ID token, checks email_verified=True,
    then creates the actual Trandia account in MongoDB.
    """
    _require_db(db)

    # Verify Firebase ID token
    try:
        decoded = await asyncio.to_thread(
            firebase_admin_auth.verify_id_token, data.firebase_id_token
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid Firebase token")

    # Must be email-verified
    if not decoded.get("email_verified"):
        raise HTTPException(
            status_code=400,
            detail="Email not verified yet. Please click the link in your email first.",
        )

    email = decoded.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Could not get email from token")

    # Check duplicates
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email is already registered")
    if await db.users.find_one({"username": data.username}):
        raise HTTPException(status_code=400, detail="Username is already taken")

    doc = {
        "name": data.name,
        "username": data.username,
        "email": email,
        "password_hash": await hash_password(data.password),
        "is_google_user": False,
        "google_id": None,
        "picture": None,
        "fcm_token": data.fcm_token,
        "email_verified": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "last_login": datetime.now(timezone.utc),
    }

    try:
        result = await db.users.insert_one(doc)
    except DuplicateKeyError as e:
        key = "email" if "email" in str(e) else "username"
        raise HTTPException(
            status_code=400,
            detail="Email is already registered" if key == "email" else "Username is already taken",
        )

    doc["_id"] = result.inserted_id
    schedule_welcome_notification(data.fcm_token, data.name, is_signup=True)
    return _build_auth_response(doc, "Account created successfully. Welcome to Trandia!")


# ── Email/Password Login ──────────────────────────────────────────────────────

async def login_with_email(data: UserLogin, db: AsyncIOMotorDatabase) -> AuthResponse:
    _require_db(db)

    user = await db.users.find_one({"email": data.email})

    if not user or not user.get("password_hash"):
        await verify_password(data.password, _DUMMY_HASH)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not await verify_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    update_fields: dict = {"last_login": datetime.now(timezone.utc)}
    if data.fcm_token:
        update_fields["fcm_token"] = data.fcm_token

    await db.users.update_one({"_id": user["_id"]}, {"$set": update_fields})
    if data.fcm_token:
        user["fcm_token"] = data.fcm_token

    schedule_welcome_notification(data.fcm_token, user["name"], is_signup=False)
    return _build_auth_response(user, "Welcome back to Trandia!")


# ── Google Auth ───────────────────────────────────────────────────────────────

async def auth_with_google_userinfo(
    userinfo: dict, fcm_token: Optional[str], db: AsyncIOMotorDatabase
) -> AuthResponse:
    _require_db(db)

    email = userinfo.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Google account has no email")

    google_id = userinfo.get("sub") or userinfo.get("id") or ""
    name = userinfo.get("name") or email.split("@")[0]
    picture = userinfo.get("picture")

    existing = await db.users.find_one({"email": email})
    if existing:
        update_fields: dict = {
            "last_login": datetime.now(timezone.utc),
            "google_id": google_id,
            "picture": picture,
            "updated_at": datetime.now(timezone.utc),
        }
        if fcm_token:
            update_fields["fcm_token"] = fcm_token

        await db.users.update_one({"_id": existing["_id"]}, {"$set": update_fields})
        existing.update({"picture": picture})
        if fcm_token:
            existing["fcm_token"] = fcm_token

        schedule_welcome_notification(fcm_token, existing["name"], is_signup=False)
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
        "email_verified": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "last_login": datetime.now(timezone.utc),
    }
    try:
        result = await db.users.insert_one(doc)
    except DuplicateKeyError as e:
        key = "email" if "email" in str(e) else "username"
        if key == "email":
            existing = await db.users.find_one({"email": email})
            if existing:
                schedule_welcome_notification(fcm_token, existing["name"], is_signup=False)
                return _build_auth_response(existing, "Welcome back to Trandia!")
        raise HTTPException(status_code=400, detail="Account already exists")

    doc["_id"] = result.inserted_id
    schedule_welcome_notification(fcm_token, name, is_signup=True)
    return _build_auth_response(doc, "Account created with Google. Welcome to Trandia!")


async def auth_with_google_id_token(
    token_str: str, fcm_token: Optional[str], db: AsyncIOMotorDatabase
) -> AuthResponse:
    _require_db(db)
    idinfo = await _verify_google_id_token(token_str)
    if not idinfo:
        raise HTTPException(status_code=401, detail="Invalid Google token")
    return await auth_with_google_userinfo(idinfo, fcm_token, db)
