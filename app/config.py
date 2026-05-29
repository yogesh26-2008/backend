from pydantic_settings import BaseSettings
from pydantic import Field, field_validator


class Settings(BaseSettings):
    mongodb_url: str
    mongodb_db: str = "trandia"

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 7

    google_client_id: str
    google_client_secret: str
    google_android_client_id: str
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    firebase_credentials_path: str = ""

    # Firebase Web API Key — used to verify ID tokens via REST API
    firebase_web_api_key: str = Field(
        default="",
        description="Load from environment, never hardcode!"
    )

    app_secret_key: str
    allowed_origins: str = ""

    brevo_api_key: str = ""

    # ── Media storage (Cloudinary by default) ────────────────────────────────
    # To swap provider: change media_provider and add provider-specific vars below.
    media_provider: str = "cloudinary"

    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""

    # ── Agora RTC (Voice & Video Calls) ──────────────────────────────────────
    agora_app_id: str = "4acf66a0e7e246fe80064783ec2bb879"
    agora_app_certificate: str = ""   # Set in .env / Railway env vars

    redis_url: str = Field(
        default="",
        description="Redis connection URL, e.g. redis://localhost:6379 or rediss://... for TLS",
    )

    # ── AI providers (Quiz feature) ───────────────────────────────────────────
    sarvam_api_key: str = ""
    groq_api_key: str = ""
    cerebras_api_key: str = ""
    sambanova_api_key: str = ""
    quiz_generation_timeout_ms: int = 10000

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
