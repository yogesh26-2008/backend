import json
import re
import urllib.parse
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
import httpx
from itsdangerous import URLSafeTimedSerializer, BadSignature

from app.config import settings
from app.database import get_db
from app.limiter import limiter
from pydantic import BaseModel, EmailStr
from app.models.user import (
    UserLogin,
    GoogleTokenRequest,
    AuthResponse,
    FirebaseSignupRequest,
)
from app.services import auth_service


class CleanupFirebaseRequest(BaseModel):
    email: EmailStr


router = APIRouter()
_signer = URLSafeTimedSerializer(settings.app_secret_key)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

_ALLOWED_ORIGINS: set[str] = set(
    o.strip() for o in settings.allowed_origins.split(",") if o.strip()
)
_LOCALHOST_RE = re.compile(r"^https?://localhost(:\d+)?$")


def _is_safe_origin(origin: str) -> bool:
    if not origin:
        return True
    if _LOCALHOST_RE.match(origin):
        return True
    if _ALLOWED_ORIGINS and origin in _ALLOWED_ORIGINS:
        return True
    return False


# ── Signup — Firebase Email Verification ─────────────────────────────────────

@router.post("/signup", response_model=AuthResponse)
@limiter.limit("3/minute")
@limiter.limit("10/hour")
async def signup(request: Request, data: FirebaseSignupRequest, db=Depends(get_db)):
    """
    Complete signup after Firebase email verification.
    Flutter verifies email via Firebase, gets ID token, sends here.
    """
    return await auth_service.signup_with_firebase_verified_email(data, db)


@router.post("/cleanup-orphaned-firebase")
@limiter.limit("5/minute")
async def cleanup_orphaned_firebase(
    request: Request, data: CleanupFirebaseRequest, db=Depends(get_db)
):
    """
    Clean up orphaned Firebase users that exist in Firebase but not in MongoDB.
    Called by Flutter when a signup fails due to email-already-in-use in Firebase
    but the email is not actually registered in our database.
    """
    return await auth_service.cleanup_orphaned_firebase_user(data.email, db)


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=AuthResponse)
@limiter.limit("5/minute")
@limiter.limit("20/hour")
async def login(request: Request, data: UserLogin, db=Depends(get_db)):
    return await auth_service.login_with_email(data, db)


# ── Google Auth ───────────────────────────────────────────────────────────────

@router.post("/google/verify", response_model=AuthResponse)
@limiter.limit("10/minute")
async def google_verify(request: Request, data: GoogleTokenRequest, db=Depends(get_db)):
    return await auth_service.auth_with_google_id_token(data.id_token, data.fcm_token, db)


@router.get("/google/web")
@limiter.limit("20/minute")
async def google_web_login(request: Request, app_origin: str = ""):
    if app_origin and not _is_safe_origin(app_origin):
        raise HTTPException(status_code=400, detail="Untrusted app_origin")

    state_payload = json.dumps({"nonce": "trandia-oauth", "app_origin": app_origin})
    state = _signer.dumps(state_payload)

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "state": state,
        "prompt": "select_account",
    }
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@router.get("/google/callback")
async def google_callback(
    code: str = None,
    state: str = None,
    error: str = None,
    db=Depends(get_db),
):
    def error_redirect(msg: str, origin: str = "") -> RedirectResponse:
        base = origin if (origin and _is_safe_origin(origin)) else ""
        return RedirectResponse(f"{base}/?error={urllib.parse.quote(msg, safe='')}")

    if error:
        return error_redirect(error)
    if not state or not code:
        return error_redirect("missing_state_or_code")

    app_origin = ""
    try:
        raw = _signer.loads(state, max_age=600)
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
                app_origin = data.get("app_origin", "")
            except Exception:
                app_origin = ""
        elif isinstance(raw, dict):
            app_origin = raw.get("app_origin", "")
    except BadSignature:
        return error_redirect("invalid_oauth_state")

    if app_origin and not _is_safe_origin(app_origin):
        return error_redirect("untrusted_redirect_origin")

    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            return error_redirect("token_exchange_failed", app_origin)

        access_token = token_resp.json().get("access_token")
        if not access_token:
            return error_redirect("no_access_token", app_origin)

        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_resp.status_code != 200:
        return error_redirect("userinfo_fetch_failed", app_origin)

    userinfo = userinfo_resp.json()
    result = await auth_service.auth_with_google_userinfo(userinfo, None, db)

    user_json = urllib.parse.quote(result.user.model_dump_json(), safe="")
    message = urllib.parse.quote(result.message, safe="")

    redirect_base = app_origin if app_origin else ""
    return RedirectResponse(
        f"{redirect_base}/?token={result.access_token}&user={user_json}&message={message}"
    )
