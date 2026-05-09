from pydantic_settings import BaseSettings
from pydantic import field_validator


class Settings(BaseSettings):
    mongodb_url: str
    mongodb_db: str = "trandia"

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    # BUG FIX: Was 10080 min (7 days) — no refresh token mechanism exists.
    # Reduced to 60 min so stolen tokens expire quickly.
    jwt_expire_minutes: int = 60

    google_client_id: str
    google_client_secret: str
    google_android_client_id: str
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    firebase_credentials_path: str = ""

    # BUG FIX: Removed hardcoded default "trandia-secret-key".
    # A predictable default makes OAuth state signing insecure.
    # This MUST be set in .env / Railway env vars.
    app_secret_key: str

    # BUG FIX: Added allowed_origins for open-redirect protection.
    # Comma-separated list of trusted Flutter web origins, e.g.:
    #   http://localhost:59236,https://trandia.app
    # Leave empty only in local dev (all localhost origins are always allowed).
    allowed_origins: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("app_secret_key")
    @classmethod
    def app_secret_must_be_strong(cls, v: str) -> str:
        if v in ("trandia-secret-key", "change-me", "secret", ""):
            raise ValueError(
                "APP_SECRET_KEY is using an insecure default. "
                "Set a strong random value in your .env or Railway env vars."
            )
        if len(v) < 16:
            raise ValueError("APP_SECRET_KEY must be at least 16 characters.")
        return v


settings = Settings()
