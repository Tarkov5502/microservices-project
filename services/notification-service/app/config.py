from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    environment: str = "development"
    servicebus_connection_string: str = ""
    servicebus_topic_tasks: str = "task-events"
    servicebus_topic_users: str = "user-events"
    servicebus_subscription_name: str = "notification-service"
    class Config:
        env_file = ".env"

settings = Settings()
