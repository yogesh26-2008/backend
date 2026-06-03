import asyncio
import bcrypt as _bcrypt
import httpx
import base64
import json as json_lib
import logging
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from typing import Optional

from bson import ObjectId
from app.config import settings
from app.models.user import UserLogin, UserResponse, AuthResponse, FirebaseSignupRequest
from app.utils.jwt_handler import create_access_token, create_refresh_token, hash_refresh_token
from app.utils.password import hash_password, verify_password
from app.services.notification_service import schedule_welcome_notification, _initialized as firebase_initialized

logger = logging.getLogger(__name__)

# Valid bcrypt hash used only for timing equalization (prevents user-enumeration via
# response-time differences). Computed once at startup with cost 4 so it adds ~5 ms.
_DUMMY_HASH: str = _bcrypt.hashpw(b"__timing_equalization__", _bcrypt.gensalt(4)).decode()


async def _build_auth_response(
    user_doc: dict, message: str, db: AsyncIOMotorDatabase
) -> AuthResponse:
    uid = str(user_doc["_id"])
    access_token = create_access_token(uid, user_doc["email"])
    refresh_token = create_refresh_token()

    # Store SHA-256 hash of refresh token in MongoDB (with 7-day TTL).
    # Raw token is returned to client; hash is what we compare on refresh.
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_expire_days)
    await db.refresh_tokens.insert_one({
        "token": hash_refresh_token(refresh_token),
        "user_id": uid,
        "created_at": datetime.now(timezone.utc),
        "expires_at": expires_at,
        "revoked": False,
    })

    user = UserResponse(
        id=uid,
        name=user_doc["name"],
        username=user_doc["username"],
        email=user_doc["email"],
        picture=user_doc.get("picture"),
        is_google_user=user_doc.get("is_google_user", False),
        created_at=user_doc["created_at"],
        followers_count=user_doc.get("followers_count", 0),
        following_count=user_doc.get("following_count", 0),
    )
    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=user,
        message=message,
    )


def _require_db(db: AsyncIOMotorDatabase):
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available. Please try again.")


def _decode_firebase_jwt_payload(token_str: str) -> dict:
    """
    Decode Firebase JWT payload WITHOUT signature verification.
    Used only as a fallback to extract email when Firebase Admin is unavailable.
    """
    try:
        parts = token_str.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid JWT format")
        # Add padding to base64
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json_lib.loads(base64.urlsafe_b64decode(payload_b64))
        return payload
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token format: {e}")


async def _verify_firebase_token(token_str: str) -> dict:
    """
    Verify Firebase ID token. Fail-closed: if both methods fail, raise 401.
    Strategy:
      1. Firebase Admin SDK  -- most secure (verifies Google public-key signature)
      2. Firebase REST API   -- fallback when Admin SDK is not initialised
    Method 3 (unsigned JWT decode) removed -- it accepted forged tokens.
    """

    # -- Method 1: Firebase Admin SDK -------------------------------------
    if firebase_initialized:
        try:
            from firebase_admin import auth as fb_auth
            decoded = await asyncio.to_thread(
                fb_auth.verify_id_token, token_str, check_revoked=False
            )
            logger.info("[AUTH] Firebase Admin token verified.")
            return {
                "email": decoded.get("email"),
                "emailVerified": decoded.get("email_verified", False),
            }
        except Exception as e:
            logger.warning(f"[AUTH] Firebase Admin verify failed: {type(e).__name__}: {e}")

    # -- Method 2: Firebase REST API --------------------------------------
    if settings.firebase_web_api_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://identitytoolkit.googleapis.com/v1/accounts:lookup"
                    f"?key={settings.firebase_web_api_key}",
                    json={"idToken": token_str},
                )
            if resp.status_code == 200:
                users = resp.json().get("users", [])
                if users:
                    logger.info("[AUTH] Firebase REST API token verified.")
                    return users[0]
            logger.warning(f"[AUTH] Firebase REST API returned status={resp.status_code}")
        except Exception as e:
            logger.warning(f"[AUTH] Firebase REST API failed: {type(e).__name__}: {e}")

    # -- Both methods failed: reject (fail-closed) -----------------------
    logger.error(
        "[AUTH] Firebase token could not be verified. "
        "Ensure FIREBASE_CREDENTIALS_PATH and FIREBASE_WEB_API_KEY are set."
    )
    raise HTTPException(
        status_code=401,
        detail="Firebase token verification failed. Please sign in again.",
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


# â"€â"€ Firebase Email Verification Signup â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

async def signup_with_firebase_verified_email(
    data: FirebaseSignupRequest, db: AsyncIOMotorDatabase
) -> AuthResponse:
    _require_db(db)

    logger.info("[AUTH] Signup attempt received")

    # Verify Firebase token (tries 3 methods)
    firebase_user = await _verify_firebase_token(data.firebase_id_token)

    if not firebase_user.get("emailVerified"):
        raise HTTPException(
            status_code=400,
            detail="Email not verified yet. Please click the verification link in your email.",
        )

    email = firebase_user.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Could not extract email from token")

    logger.info("[AUTH] Firebase email verified")

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
    logger.info("[AUTH] Account created successfully")
    schedule_welcome_notification(data.fcm_token, data.name, is_signup=True)
    return await _build_auth_response(doc, "Account created successfully. Welcome to Trandia!", db)


# â"€â"€ Email/Password Login â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

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

    notif_master = user.get("notification_settings", {}).get("master", True)
    schedule_welcome_notification(data.fcm_token, user["name"], is_signup=False, master_enabled=notif_master)
    return await _build_auth_response(user, "Welcome back to Trandia!", db)


# â"€â"€ Google Auth â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

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
        notif_master = existing.get("notification_settings", {}).get("master", True)
        schedule_welcome_notification(fcm_token, existing["name"], is_signup=False, master_enabled=notif_master)
        return await _build_auth_response(existing, "Welcome back to Trandia!", db)

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
                notif_master = existing.get("notification_settings", {}).get("master", True)
                schedule_welcome_notification(fcm_token, existing["name"], is_signup=False, master_enabled=notif_master)
                return await _build_auth_response(existing, "Welcome back to Trandia!", db)
        raise HTTPException(status_code=400, detail="Account already exists")

    doc["_id"] = result.inserted_id
    schedule_welcome_notification(fcm_token, name, is_signup=True)
    return await _build_auth_response(doc, "Account created with Google. Welcome to Trandia!", db)


async def cleanup_orphaned_firebase_user(
    email: str, db: AsyncIOMotorDatabase
) -> dict:
    """
    If a Firebase user exists for this email but NO MongoDB account exists,
    delete the Firebase user so the client can retry signup cleanly.

    Returns {"cleaned": True/False, "message": str}
    """
    _require_db(db)

    # If the email IS in MongoDB, it's a real account -- don't touch it.
    existing = await db.users.find_one({"email": email}, projection={"_id": 1})
    if existing:
        raise HTTPException(
            status_code=409,
            detail="This email is already registered. Please sign in instead.",
        )

    # Email NOT in MongoDB â†' orphaned Firebase user. Delete it.
    # Method 1: Firebase Admin SDK
    if firebase_initialized:
        try:
            from firebase_admin import auth as fb_auth
            fb_user = await asyncio.to_thread(fb_auth.get_user_by_email, email)
            await asyncio.to_thread(fb_auth.delete_user, fb_user.uid)
            logger.info("[AUTH] Orphaned Firebase user cleaned up")
            return {"cleaned": True, "message": "Orphaned account cleaned up. Please sign up again."}
        except Exception as e:
            print(f"[AUTH] âš ï¸ Admin SDK cleanup failed: {type(e).__name__}: {e}")
            # Fall through to REST API

    # Method 2: Firebase REST API -- can't delete users, but we can confirm the orphan
    # Since REST API can't delete users without Admin SDK, we return a message
    # telling the client to proceed with a password reset flow or retry
    logger.info("[AUTH] Orphaned Firebase user confirmed (no MongoDB record)")
    return {"cleaned": False, "message": "Account not found in our system. Firebase Admin unavailable for cleanup."}


async def auth_with_google_id_token(
    token_str: str, fcm_token: Optional[str], db: AsyncIOMotorDatabase
) -> AuthResponse:
    _require_db(db)
    idinfo = await _verify_google_id_token(token_str)
    if not idinfo:
        raise HTTPException(status_code=401, detail="Invalid Google token")
    return await auth_with_google_userinfo(idinfo, fcm_token, db)


# ── Token Refresh ─────────────────────────────────────────────────────────────

async def refresh_access_token(
    refresh_token_str: str, db: AsyncIOMotorDatabase
) -> AuthResponse:
    """
    Validate a refresh token, rotate it (revoke old, issue new), and return
    a fresh access_token + refresh_token pair.

    Rotation strategy:
      - Old refresh token is marked revoked immediately.
      - A brand-new refresh token is issued alongside the new access token.
      - Expired or revoked tokens are rejected with HTTP 401.
    """
    _require_db(db)

    now = datetime.now(timezone.utc)

    # Look up token by hash — must exist, not revoked, not expired
    token_doc = await db.refresh_tokens.find_one({
        "token": hash_refresh_token(refresh_token_str),
        "revoked": False,
        "expires_at": {"$gt": now},
    })

    if not token_doc:
        raise HTTPException(
            status_code=401,
            detail="Refresh token is invalid or has expired. Please sign in again."
        )

    user_id = token_doc["user_id"]

    # Load user
    try:
        user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        user_doc = None

    if not user_doc:
        raise HTTPException(
            status_code=401,
            detail="Account not found. Please sign in again."
        )

    # Revoke the old refresh token (rotation — prevents reuse)
    await db.refresh_tokens.update_one(
        {"_id": token_doc["_id"]},
        {"$set": {"revoked": True, "revoked_at": now}}
    )

    logger.info("[AUTH] Refresh token rotated")
    return await _build_auth_response(user_doc, "Token refreshed successfully.", db)


async def revoke_refresh_token(refresh_token_str: str, db: AsyncIOMotorDatabase) -> None:
    """
    Revoke a specific refresh token (called on logout).
    Silently ignores unknown tokens so logout never fails.
    """
    if not refresh_token_str or db is None:
        return
    await db.refresh_tokens.update_one(
        {"token": hash_refresh_token(refresh_token_str)},
        {"$set": {"revoked": True, "revoked_at": datetime.now(timezone.utc)}}
    )

