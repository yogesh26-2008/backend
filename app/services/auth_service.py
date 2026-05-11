import asyncio
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from typing import Optional

from app.config import settings
from app.models.user import UserCreate, UserLogin, UserResponse, AuthResponse, SignupInitiateResponse
from app.utils.jwt_handler import create_access_token
from app.utils.password import hash_password, verify_password
from app.utils.otp import generate_otp, otp_expiry, MAX_OTP_ATTEMPTS
from app.utils.email_service import send_otp_email
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


# ── Email Verification ────────────────────────────────────────────────────────

async def initiate_signup(data: UserCreate, db: AsyncIOMotorDatabase) -> SignupInitiateResponse:
    """
    Step 1 of email signup:
    - Validate email/username uniqueness
    - Generate OTP
    - Store pending verification in DB
    - Send OTP email
    - Return pending response (NO account created yet)
    """
    _require_db(db)

    if await db.users.find_one({"email": data.email}):
        raise HTTPException(status_code=400, detail="Email is already registered")
    if await db.users.find_one({"username": data.username}):
        raise HTTPException(status_code=400, detail="Username is already taken")

    otp = generate_otp()
    expires_at = otp_expiry()

    # Upsert: if user tries to initiate signup again with same email,
    # replace the old OTP with a fresh one.
    await db.email_verifications.replace_one(
        {"email": data.email},
        {
            "email": data.email,
            "otp": otp,
            "name": data.name,
            "username": data.username,
            "password_hash": await hash_password(data.password),
            "fcm_token": data.fcm_token,
            "attempts": 0,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )

    # Send OTP email (non-blocking: errors are logged, not raised)
    try:
        await send_otp_email(data.email, otp, data.name)
    except Exception as e:
        print(f"[EMAIL] ❌ Failed to send OTP to {data.email}: {e}")
        raise HTTPException(
            status_code=500,
            detail="Could not send verification email. Check SMTP config or try again.",
        )

    return SignupInitiateResponse(
        message="OTP sent to your email. Please verify to complete signup.",
        email=data.email,
    )


async def verify_email_otp(email: str, otp: str, db: AsyncIOMotorDatabase) -> AuthResponse:
    """
    Step 2 of email signup:
    - Validate OTP
    - Create actual user account
    - Delete pending verification doc
    - Return auth token
    """
    _require_db(db)

    pending = await db.email_verifications.find_one({"email": email})

    if not pending:
        raise HTTPException(
            status_code=400,
            detail="No pending verification found. Please signup again.",
        )

    # Check expiry manually (TTL index may not fire instantly)
    if datetime.now(timezone.utc) > pending["expires_at"].replace(tzinfo=timezone.utc):
        await db.email_verifications.delete_one({"email": email})
        raise HTTPException(status_code=400, detail="OTP has expired. Please signup again.")

    # Check max attempts
    if pending.get("attempts", 0) >= MAX_OTP_ATTEMPTS:
        await db.email_verifications.delete_one({"email": email})
        raise HTTPException(
            status_code=429,
            detail="Too many wrong attempts. Please signup again.",
        )

    # Verify OTP
    if pending["otp"] != otp:
        await db.email_verifications.update_one(
            {"email": email},
            {"$inc": {"attempts": 1}},
        )
        remaining = MAX_OTP_ATTEMPTS - pending.get("attempts", 0) - 1
        raise HTTPException(
            status_code=400,
            detail=f"Wrong OTP. {remaining} attempts remaining.",
        )

    # OTP is correct — check duplicates one more time (race condition guard)
    if await db.users.find_one({"email": email}):
        await db.email_verifications.delete_one({"email": email})
        raise HTTPException(status_code=400, detail="Email is already registered")

    if await db.users.find_one({"username": pending["username"]}):
        await db.email_verifications.delete_one({"email": email})
        raise HTTPException(
            status_code=400,
            detail="Username was just taken. Please signup again with a different username.",
        )

    # Create the actual user
    doc = {
        "name": pending["name"],
        "username": pending["username"],
        "email": email,
        "password_hash": pending["password_hash"],
        "is_google_user": False,
        "google_id": None,
        "picture": None,
        "fcm_token": pending.get("fcm_token"),
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

    # Clean up pending verification
    await db.email_verifications.delete_one({"email": email})

    doc["_id"] = result.inserted_id
    schedule_welcome_notification(pending.get("fcm_token"), pending["name"], is_signup=True)
    return _build_auth_response(doc, "Account created successfully. Welcome to Trandia!")


async def resend_otp(email: str, db: AsyncIOMotorDatabase) -> SignupInitiateResponse:
    """Replace the old OTP with a fresh one and resend."""
    _require_db(db)

    pending = await db.email_verifications.find_one({"email": email})
    if not pending:
        raise HTTPException(
            status_code=400,
            detail="No pending signup found for this email. Please signup again.",
        )

    otp = generate_otp()
    await db.email_verifications.update_one(
        {"email": email},
        {"$set": {"otp": otp, "attempts": 0, "expires_at": otp_expiry()}},
    )

    try:
        await send_otp_email(email, otp, pending["name"])
    except Exception as e:
        print(f"[EMAIL] ❌ Failed to resend OTP to {email}: {e}")
        raise HTTPException(status_code=500, detail="Could not resend OTP. Try again.")

    return SignupInitiateResponse(
        message="A new OTP has been sent to your email.",
        email=email,
    )


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
