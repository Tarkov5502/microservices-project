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

    # ── JWT key rotation (optional) ─────────────────────────────────────────
    # When jwt_secrets is populated, the service uses it as a keyring keyed
    # by `kid`. Format: "kid1=secret1,kid2=secret2" (no spaces in secrets).
    # When non-empty, jwt_current_kid points at the kid used for NEW tokens.
    # All listed kids remain accepted at verification time, which lets old
    # tokens continue to work through a rotation window.
    # When empty, the service falls back to the single jwt_secret above
    # (implicit kid "default") — backwards compatible.
    jwt_secrets: str = ""
    jwt_current_kid: str = "default"

    # ── Database connection pool ─────────────────────────────────────────────
    # Per-process pool sizes. Multiply by HPA max replicas to get the peak
    # concurrent connections this service might open. A B1ms Postgres caps
    # at ~50 connections, so dev should run with smaller pools (set in K8s/
    # docker-compose env). Production GP_Standard_D2s_v3 supports ~200.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 10
    db_pool_recycle: int = 3600

    # ── Email + verification + password reset ─────────────────────────────
    # Sender backend: "log" (default; just logs to stdout for dev) or "smtp"
    # (production — set the smtp_* fields below).
    email_sender: str = "log"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_address: str = "no-reply@example.com"

    # Public base URL of the frontend. Used to build verification + reset
    # links delivered by email. Must be a real reachable URL in production;
    # in dev this just controls what the logged link looks like.
    public_base_url: str = "http://localhost:3000"

    # Token lifetimes
    email_verification_token_ttl_seconds: int = 60 * 60 * 24   # 24 h
    password_reset_token_ttl_seconds: int = 60 * 60            # 1 h

    # ── Inter-service identity HMAC ─────────────────────────────────────────
    # Symmetric secret shared with the api-gateway (synced from Key Vault).
    # We verify the gateway's signature over X-User-* headers; without a valid
    # signature, requests with identity claims are rejected even if they reach
    # us. Defends against NetworkPolicy misconfiguration / lateral movement.
    interservice_hmac_secret: str = "dev-only-interservice-secret-change-in-production-please"

    # ── Admin bootstrap ──────────────────────────────────────────────────────
    # On startup, if INITIAL_ADMIN_EMAIL is set and a user with that email
    # exists, they are promoted to admin. Idempotent: already-admin users are
    # left alone; missing users are logged and skipped. Intended to bridge the
    # "you can't promote a user without already being an admin" chicken/egg
    # gap on a fresh deployment. Unset this env var after first use.
    initial_admin_email: str | None = None

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
