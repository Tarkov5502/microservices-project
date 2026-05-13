from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    environment: str = "development"
    servicebus_connection_string: str = ""
    servicebus_topic_tasks: str = "task-events"
    servicebus_topic_users: str = "user-events"
    servicebus_subscription_name: str = "notification-service"

    # ── Inter-service identity HMAC ─────────────────────────────────────────
    # Shared secret with the api-gateway used to verify signed X-User-*
    # headers on the SSE stream endpoint.
    interservice_hmac_secret: str = "dev-only-interservice-secret-change-in-production-please"

    class Config:
        env_file = ".env"

settings = Settings()
