"""
api-gateway/app/config.py

SECURITY NOTES:
  - jwt_algorithm is validated against an explicit allowlist of secure algorithms.
    Allowing arbitrary algorithm values (especially "none") enables trivial auth bypass.
  - jwt_secret is validated for minimum entropy at startup. Deploying with a default
    or short secret allows any attacker who knows the source code to forge tokens.
  - allowed_origins defaults to empty (deny all) rather than wildcard. Callers MUST
    set this explicitly. Additionally, allow_credentials=True is incompatible with
    wildcard origins in the CORS spec (Starlette 0.37 raises ValueError on startup).
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings

# Algorithms we'll accept. "none" is explicitly omitted — it disables
# signature verification entirely. RS*/ES* are fine too but not listed
# because this service uses symmetric HS256 by default.
_ALLOWED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})

# Absolute minimum secret length (bytes). Shorter keys are brute-forceable.
_MIN_JWT_SECRET_LEN = 32

# Defaults that must never appear in production.
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

    # ── JWT ──────────────────────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"

    # ── Upstream URLs ─────────────────────────────────────────────────────────
    user_service_url: str = "http://user-service:8001"
    task_service_url: str = "http://task-service:8002"
    notification_service_url: str = "http://notification-service:8003"

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Intentionally no wildcard default. Set this per environment.
    # Do NOT combine ["*"] with allow_credentials=True — Starlette rejects it.
    allowed_origins: list[str] = []

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60

    class Config:
        env_file = ".env"
        case_sensitive = False

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("jwt_algorithm")
    @classmethod
    def algorithm_must_be_secure(cls, v: str) -> str:
        if v not in _ALLOWED_JWT_ALGORITHMS:
            raise ValueError(
                f"jwt_algorithm '{v}' is not in the allowed list {sorted(_ALLOWED_JWT_ALGORITHMS)}. "
                f"'none' and asymmetric algorithms are not supported by this service."
            )
        return v

    @field_validator("jwt_secret")
    @classmethod
    def secret_must_be_strong(cls, v: str) -> str:
        if v.lower() in _BANNED_SECRETS:
            raise ValueError(
                "jwt_secret is set to a known-weak default value. "
                "Generate a strong random secret with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if len(v) < _MIN_JWT_SECRET_LEN:
            raise ValueError(
                f"jwt_secret must be at least {_MIN_JWT_SECRET_LEN} characters. "
                f"Current length: {len(v)}."
            )
        return v


settings = Settings()
