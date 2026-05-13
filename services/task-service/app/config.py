from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: str = "development"
    database_url: str = "postgresql://user:pass@localhost:5432/appdb"
    redis_url: str = "redis://localhost:6379"
    servicebus_connection_string: str = ""
    servicebus_topic_tasks: str = "task-events"

    # ── Inter-service identity HMAC ─────────────────────────────────────────
    # Shared secret with the api-gateway used to verify signed X-User-*
    # headers. See user-service/app/config.py for the rationale.
    interservice_hmac_secret: str = "dev-only-interservice-secret-change-in-production-please"

    # ── Database connection pool ─────────────────────────────────────────────
    # See user-service/app/config.py for the rationale on these knobs. Mirror
    # the defaults so both services scale the same way unless explicitly
    # overridden per environment.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 10
    db_pool_recycle: int = 3600

    class Config:
        env_file = ".env"


settings = Settings()
