"""
api-gateway/app/config.py — Configuration via environment variables.

Best practice: Never hardcode config. Use env vars so the same Docker image
works in dev, staging, and prod — only the env vars change.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: str = "development"
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"

    user_service_url: str = "http://user-service:8001"
    task_service_url: str = "http://task-service:8002"
    notification_service_url: str = "http://notification-service:8003"

    allowed_origins: list[str] = ["*"]

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
