from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongodb_url: str
    mongodb_db: str = "trandia"

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 10080

    google_client_id: str
    google_client_secret: str
    google_android_client_id: str
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    firebase_credentials_path: str = ""
    app_secret_key: str = "trandia-secret-key"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
