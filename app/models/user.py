from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional
from datetime import datetime


class UserCreate(BaseModel):
    name: str
    username: str
    email: EmailStr
    password: str
    fcm_token: Optional[str] = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v

    @field_validator("username")
    @classmethod
    def username_clean(cls, v):
        v = v.strip().lstrip("@").lower()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        return v


class UserLogin(BaseModel):
    email: EmailStr
    password: str
    fcm_token: Optional[str] = None


class GoogleTokenRequest(BaseModel):
    id_token: str
    fcm_token: Optional[str] = None


class FCMTokenUpdate(BaseModel):
    fcm_token: str


class UserResponse(BaseModel):
    id: str
    name: str
    username: str
    email: str
    picture: Optional[str] = None
    is_google_user: bool
    created_at: datetime
    public_key: Optional[str] = None
    followers_count: int = 0
    following_count: int = 0


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str = ""          # opaque token stored in MongoDB, 7-day TTL
    token_type: str = "bearer"
    user: UserResponse
    message: str


# ── Firebase Email Verification Signup ───────────────────────────────────────

class FirebaseSignupRequest(BaseModel):
    """
    Sent after Firebase email verification is complete.
    Flutter gets the Firebase ID token (which has email_verified=true)
    and sends it here along with the profile data.
    """
    firebase_id_token: str
    name: str
    username: str
    password: str
    fcm_token: Optional[str] = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v

    @field_validator("username")
    @classmethod
    def username_clean(cls, v):
        v = v.strip().lstrip("@").lower()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        return v

