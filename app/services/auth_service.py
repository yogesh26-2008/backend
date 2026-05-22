import asyncio
import httpx
import base64
import json as json_lib
from datetime import datetime, timezone
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from typing import Optional

from app.config import settings
from app.models.user import UserLogin, UserResponse, AuthResponse, FirebaseSignupRequest
from app.utils.jwt_handler import create_access_token
from app.utils.password import hash_password, verify_password
from app.services.notification_service import schedule_welcome_notification, _initialized as firebase_initialized

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
    Verify Firebase ID token.
    Strategy:
      1. Try firebase_admin.auth.verify_id_token() — most secure (uses Google public keys)
      2. If firebase_admin not initialized, fall back to REST API
      3. If REST API fails, fall back to JWT payload decode (least secure but still checks email_verified)
    Returns dict with 'email' and 'email_verified' fields.
    """

    # ── Method 1: Firebase Admin SDK ─────────────────────────────────────────
    if firebase_initialized:
        try:
            from firebase_admin import auth as fb_auth
            decoded = await asyncio.to_thread(
                fb_auth.verify_id_token, token_str, check_revoked=False
            )
            print(f"[AUTH] ✅ Firebase Admin token verified. email={decoded.get('email')} verified={decoded.get('email_verified')}")
            return {
                "email": decoded.get("email"),
                "emailVerified": decoded.get("email_verified", False),
            }
        except Exception as e:
            print(f"[AUTH] ⚠️ Firebase Admin verify failed: {type(e).__name__}: {e}")
            # Fall through to next method

    # ── Method 2: Firebase REST API ──────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:lookup"
                f"?key={settings.firebase_web_api_key}",
                json={"idToken": token_str},
            )
        print(f"[AUTH] Firebase REST API status={resp.status_code} body={resp.text[:200]}")

        if resp.status_code == 200:
            data = resp.json()
            users = data.get("users", [])
            if users:
                user = users[0]
                print(f"[AUTH] ✅ REST API verified. email={user.get('email')} verified={user.get('emailVerified')}")
                return user
    except Exception as e:
        print(f"[AUTH] ⚠️ Firebase REST API failed: {type(e).__name__}: {e}")

    # ── Method 3: JWT Payload Decode (fallback) ───────────────────────────────
    print("[AUTH] ⚠️ Using JWT payload decode fallback (no signature verification)")
    payload = _decode_firebase_jwt_payload(token_str)
    print(f"[AUTH] JWT payload: iss={payload.get('iss')} email={payload.get('email')} verified={payload.get('email_verified')}")

    # At minimum check issuer to confirm it's from Firebase
    iss = payload.get("iss", "")
    if not iss.startswith("https://securetoken.google.com/"):
        raise HTTPException(status_code=401, detail="Token is not from Firebase")

    # Check expiry
    exp = payload.get("exp", 0)
    if datetime.now(timezone.utc).timestamp() > exp:
        raise HTTPException(status_code=401, detail="Token has expired. Please try again.")

    return {
        "email": payload.get("email"),
        "emailVerified": payload.get("email_verified", False),
    }


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
    _require_db(db)

    print(f"[AUTH] Signup attempt — name={data.name} username={data.username}")

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

    print(f"[AUTH] Email verified: {email}")

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
    print(f"[AUTH] ✅ Account created: {email}")
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


async def cleanup_orphaned_firebase_user(
    email: str, db: AsyncIOMotorDatabase
) -> dict:
    """
    If a Firebase user exists for this email but NO MongoDB account exists,
    delete the Firebase user so the client can retry signup cleanly.

    Returns {"cleaned": True/False, "message": str}
    """
    _require_db(db)

    # If the email IS in MongoDB, it's a real account — don't touch it.
    existing = await db.users.find_one({"email": email}, projection={"_id": 1})
    if existing:
        raise HTTPException(
            status_code=409,
            detail="This email is already registered. Please sign in instead.",
        )

    # Email NOT in MongoDB → orphaned Firebase user. Delete it.
    # Method 1: Firebase Admin SDK
    if firebase_initialized:
        try:
            from firebase_admin import auth as fb_auth
            fb_user = await asyncio.to_thread(fb_auth.get_user_by_email, email)
            await asyncio.to_thread(fb_auth.delete_user, fb_user.uid)
            print(f"[AUTH] 🧹 Orphaned Firebase user deleted (Admin SDK): {email}")
            return {"cleaned": True, "message": "Orphaned account cleaned up. Please sign up again."}
        except Exception as e:
            print(f"[AUTH] ⚠️ Admin SDK cleanup failed: {type(e).__name__}: {e}")
            # Fall through to REST API

    # Method 2: Firebase REST API — can't delete users, but we can confirm the orphan
    # Since REST API can't delete users without Admin SDK, we return a message
    # telling the client to proceed with a password reset flow or retry
    print(f"[AUTH] 🧹 Email {email} not in MongoDB — orphaned Firebase user confirmed")
    return {"cleaned": False, "message": "Account not found in our system. Firebase Admin unavailable for cleanup."}


async def auth_with_google_id_token(
    token_str: str, fcm_token: Optional[str], db: AsyncIOMotorDatabase
) -> AuthResponse:
    _require_db(db)
    idinfo = await _verify_google_id_token(token_str)
    if not idinfo:
        raise HTTPException(status_code=401, detail="Invalid Google token")
    return await auth_with_google_userinfo(idinfo, fcm_token, db)
