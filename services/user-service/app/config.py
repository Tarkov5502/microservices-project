"""
user-service/app/config.py

JWT defaults MUST match api-gateway's defaults exactly. Mismatched secrets
between the services that issue and the service that verifies tokens causes
all tokens to be silently rejected, breaking the entire auth flow.

The same validation rules as the gateway apply here — algorithm allowlist and
minimum secret entropy. The user-service is the only one that ISSUES tokens;
if it runs with a weak secret, every token it generates is compromised.
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings

_ALLOWED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})
_MIN_JWT_SECRET_LEN = 32
_BANNED_SECRETS = frozenset({
    "change-me",
    "change-me-in-production",
    "secret",
    "password",
    "jwt-secret",
    "your-secret",
})


class Settings(BaseSettings):
    environment: str = "development"
    database_url: str = "postgresql://user:pass@localhost:5432/appdb"
    redis_url: str = "redis://localhost:6379"

    # MUST match the api-gateway's jwt_secret exactly — same default value
    # so local development works out of the box with both services unset.
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60

    class Config:
        env_file = ".env"
        case_sensitive = False

    @field_validator("jwt_algorithm")
    @classmethod
    def algorithm_must_be_secure(cls, v: str) -> str:
        if v not in _ALLOWED_JWT_ALGORITHMS:
            raise ValueError(
                f"jwt_algorithm '{v}' is not allowed. "
                f"Permitted values: {sorted(_ALLOWED_JWT_ALGORITHMS)}"
            )
        return v

    @field_validator("jwt_secret")
    @classmethod
    def secret_must_be_strong(cls, v: str, info) -> str:
        # Skip validation in development to allow zero-config local startup.
        # In any deployed environment this MUST be overridden.
        import os
        if os.getenv("ENVIRONMENT", "development").lower() == "development":
            return v
        if v.lower() in _BANNED_SECRETS or len(v) < _MIN_JWT_SECRET_LEN:
            raise ValueError(
                f"jwt_secret is too weak for a non-development environment. "
                f"Set JWT_SECRET to a random string of at least {_MIN_JWT_SECRET_LEN} chars. "
                f"Generate one: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v


settings = Settings()
