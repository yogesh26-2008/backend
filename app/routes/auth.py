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
from app.models.user import UserCreate, UserLogin, GoogleTokenRequest, AuthResponse
from app.services import auth_service

router = APIRouter()
_signer = URLSafeTimedSerializer(settings.app_secret_key)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Open-redirect protection: only trust origins from the whitelist + localhost.
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


# ── BUG FIX: Rate limiting was defined in limiter.py but NEVER applied to
# any endpoint. All auth routes below now have strict limits.
# Limits chosen to stop brute-force and credential-stuffing attacks while
# being invisible to legitimate users:
#   - signup:         5 per minute  (new accounts take time in real use)
#   - login:         10 per minute  (fast typers + password managers)
#   - google/verify: 10 per minute  (same as login)
#   - google/web:    20 per minute  (browser redirect, less sensitive)
# ─────────────────────────────────────────────────────────────────────────

@router.post("/signup", response_model=AuthResponse)
@limiter.limit("5/minute")
async def signup(request: Request, data: UserCreate, db=Depends(get_db)):
    return await auth_service.signup_with_email(data, db)


@router.post("/login", response_model=AuthResponse)
@limiter.limit("10/minute")
async def login(request: Request, data: UserLogin, db=Depends(get_db)):
    return await auth_service.login_with_email(data, db)


@router.post("/google/verify", response_model=AuthResponse)
@limiter.limit("10/minute")
async def google_verify(request: Request, data: GoogleTokenRequest, db=Depends(get_db)):
    """Verify a Google ID token from the Flutter mobile app."""
    return await auth_service.auth_with_google_id_token(data.id_token, data.fcm_token, db)


@router.get("/google/web")
@limiter.limit("20/minute")
async def google_web_login(request: Request, app_origin: str = ""):
    """
    Redirect browser to Google consent screen.
    app_origin: the Flutter web app's origin (e.g. http://localhost:59236)
    so the callback knows where to redirect back after auth.
    """
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
    """Handle Google OAuth2 callback and redirect back to Flutter app."""

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
