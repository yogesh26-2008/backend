import json
import urllib.parse
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
import httpx
from itsdangerous import URLSafeTimedSerializer, BadSignature

from app.config import settings
from app.database import get_db
from app.models.user import UserCreate, UserLogin, GoogleTokenRequest, AuthResponse
from app.services import auth_service

router = APIRouter()
_signer = URLSafeTimedSerializer(settings.app_secret_key)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


@router.post("/signup", response_model=AuthResponse)
async def signup(data: UserCreate, db=Depends(get_db)):
    return await auth_service.signup_with_email(data, db)


@router.post("/login", response_model=AuthResponse)
async def login(data: UserLogin, db=Depends(get_db)):
    return await auth_service.login_with_email(data, db)


@router.post("/google/verify", response_model=AuthResponse)
async def google_verify(data: GoogleTokenRequest, db=Depends(get_db)):
    """Verify a Google ID token from the Flutter mobile app."""
    return await auth_service.auth_with_google_id_token(data.id_token, data.fcm_token, db)


@router.get("/google/web")
async def google_web_login(app_origin: str = ""):
    """
    Redirect browser to Google consent screen.
    app_origin: the Flutter web app's origin (e.g. http://localhost:59236)
    so the callback knows where to redirect back after auth.
    """
    # Encode app_origin into state so we can retrieve it in the callback
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

    # Helper to build error redirect — back to app_origin if available
    def error_redirect(msg: str, origin: str = "") -> RedirectResponse:
        base = origin if origin else ""
        return RedirectResponse(f"{base}/?error={urllib.parse.quote(msg)}")

    if error:
        return error_redirect(error)

    if not state or not code:
        return error_redirect("missing_state_or_code")

    # Decode state and extract app_origin
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

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
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
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_resp.status_code != 200:
        return error_redirect("userinfo_fetch_failed", app_origin)

    userinfo = userinfo_resp.json()
    result = await auth_service.auth_with_google_userinfo(userinfo, None, db)

    user_json = urllib.parse.quote(result.user.model_dump_json())
    message = urllib.parse.quote(result.message)

    # Redirect back to Flutter app if app_origin provided, else backend root
    redirect_base = app_origin if app_origin else ""
    return RedirectResponse(
        f"{redirect_base}/?token={result.access_token}&user={user_json}&message={message}"
    )
