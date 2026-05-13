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
import json

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings

# Algorithms we will accept. "none" is explicitly omitted — it disables
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


def _parse_origins(raw: str) -> list[str]:
    """
    Accept comma-separated strings as well as JSON arrays.

    Operators reach for `ALLOWED_ORIGINS=https://a.com,https://b.com` because
    that is the shape they use everywhere else. The default pydantic-settings
    parser for list[str] fields only accepts a JSON-encoded list and crashes
    with SettingsError otherwise — which we worked around by typing the raw
    field as a string and parsing it here.
    """
    if not raw:
        return []
    s = raw.strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            decoded = json.loads(s)
            if isinstance(decoded, list):
                return [str(x).strip() for x in decoded if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return [item.strip() for item in s.split(",") if item.strip()]


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
    # Raw value read from the ALLOWED_ORIGINS env var. Typed as str (not
    # list[str]) so pydantic-settings does NOT try to JSON-decode it at parse
    # time — that decode was crashing the service whenever an operator set
    # `ALLOWED_ORIGINS=https://app.example.com` (the natural form).
    # Read settings.allowed_origins (the property below) to consume as a list.
    # Do NOT combine ["*"] with allow_credentials=True — Starlette rejects it.
    allowed_origins_raw: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ALLOWED_ORIGINS",
            "allowed_origins",
            "ALLOWED_ORIGINS_RAW",
            "allowed_origins_raw",
        ),
    )

    # ── Rate limiting ──────────────────────────────────────────────────
    rate_limit_requests: int = 100        # General limit per IP per window
    rate_limit_window_seconds: int = 60
    # Tighter limit for login/register — prevents password brute-force.
    # 10 attempts/min = still comfortable for real users, painful for bots.
    auth_rate_limit_requests: int = 10

    # ── Redis ─────────────────────────────────────────────────────────────────
    # Used by the rate limiter to share state across multiple gateway replicas.
    # Without Redis, each replica has an independent in-memory bucket, so with
    # N replicas a client can make N × rate_limit_requests requests per window.
    # When unset, the rate limiter falls back to in-memory (single-replica safe).
    redis_url: str = "redis://localhost:6379"

    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def allowed_origins(self) -> list[str]:
        """Parsed list of CORS-allowed origins."""
        return _parse_origins(self.allowed_origins_raw)

    @field_validator("allowed_origins_raw", mode="before")
    @classmethod
    def coerce_to_string(cls, v):
        """Normalise any input to a string before storage."""
        if v is None:
            return ""
        if isinstance(v, list):
            # A JSON-array env value (or a programmatically-passed list) is
            # joined back into the canonical comma-separated form.
            return ",".join(str(x) for x in v)
        return str(v)

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
