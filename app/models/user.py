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


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
    message: str


# ── Email Verification Models ─────────────────────────────────────────────────

class SignupInitiateResponse(BaseModel):
    """Returned after step-1 of signup — OTP has been sent."""
    message: str
    email: str


class VerifyEmailRequest(BaseModel):
    """Sent by Flutter in step-2 — user enters the OTP they received."""
    email: EmailStr
    otp: str

    @field_validator("otp")
    @classmethod
    def otp_must_be_digits(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 6:
            raise ValueError("OTP must be exactly 6 digits")
        return v


class ResendOtpRequest(BaseModel):
    email: EmailStr
