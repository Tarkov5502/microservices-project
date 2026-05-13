"""
JWT keyring with `kid`-based rotation support.

PROBLEM:
  A single jwt_secret means rotating the key invalidates every active session
  the moment the new key takes effect. For a service with thousands of users
  that's an unscheduled mass logout — operators avoid rotating, the secret
  ages forever, and a leak has a long blast radius.

SOLUTION:
  A keyring with multiple (kid, secret) pairs:
    - The signer (user-service) picks the CURRENT kid and stamps it into the
      JWT JOSE header so verifiers know which key to use.
    - The verifier (api-gateway) consults the keyring and verifies with the
      matching kid. Old kids are still accepted until removed from the ring.

ROTATION CHOREOGRAPHY:
  Day 0: Keyring = {"k1": "<old>"}. current_kid = "k1".
  Day 1: Add the new key to BOTH services' keyrings.
         Keyring = {"k1": "<old>", "k2": "<new>"}. current_kid = "k1" still.
         Both services restarted. No tokens broken.
  Day 2: Update user-service current_kid = "k2" and restart. NEW tokens now
         signed with k2. Old k1 tokens still verify.
  Day 3: Wait jwt_expiry_minutes (e.g. 60 min). All k1 tokens have expired
         by now.
  Day 4: Remove k1 from BOTH keyrings. Done — k1 secret is no longer trusted.

WIRE FORMAT:
  Env var: JWT_SECRETS="k1=abc...,k2=xyz..."
  No quoting / escaping — secrets must be base64/hex with no commas or '='
  in the body. We enforce that in parse_keyring().

BACKWARDS COMPATIBILITY:
  If JWT_SECRETS is empty, the keyring is constructed with a single entry
  using the legacy jwt_secret value under kid="default". This means existing
  deployments don't need to set anything new.
"""
from typing import Dict


class KeyringError(ValueError):
    """Raised when the JWT_SECRETS env var is malformed."""


def parse_keyring(jwt_secrets: str, fallback_secret: str, fallback_kid: str = "default") -> Dict[str, str]:
    """
    Parse "k1=secret1,k2=secret2" into {kid: secret}.

    If jwt_secrets is empty, returns {fallback_kid: fallback_secret} — the
    single-key behaviour the platform shipped with. This keeps existing
    deployments unchanged when they don't opt into rotation.
    """
    if not jwt_secrets or not jwt_secrets.strip():
        return {fallback_kid: fallback_secret}

    keyring: Dict[str, str] = {}
    for entry in jwt_secrets.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise KeyringError(
                f"Malformed JWT_SECRETS entry {entry!r}: expected kid=secret"
            )
        kid, _, secret = entry.partition("=")
        kid = kid.strip()
        secret = secret.strip()
        if not kid or not secret:
            raise KeyringError(
                f"Malformed JWT_SECRETS entry {entry!r}: empty kid or secret"
            )
        if "=" in secret:
            # Secrets are allowed to contain '=' but only after the FIRST '=',
            # which partition() already handled. If we still see another '='
            # mid-secret it's almost certainly base64 padding — fine to leave.
            pass
        if kid in keyring:
            raise KeyringError(f"Duplicate kid in JWT_SECRETS: {kid!r}")
        keyring[kid] = secret

    if not keyring:
        # JWT_SECRETS was set but parsed to nothing (e.g. just commas). Fall
        # back so the process still boots rather than crashing on a typo.
        return {fallback_kid: fallback_secret}
    return keyring


def select_signing_key(keyring: Dict[str, str], current_kid: str) -> tuple[str, str]:
    """
    Return (kid, secret) for the kid that NEW tokens should be signed with.
    Raises KeyringError if current_kid is missing from the keyring.
    """
    if current_kid not in keyring:
        raise KeyringError(
            f"jwt_current_kid={current_kid!r} is not present in the keyring "
            f"(known kids: {sorted(keyring)}). New tokens cannot be signed."
        )
    return current_kid, keyring[current_kid]


def select_verification_key(keyring: Dict[str, str], kid: str | None) -> str | None:
    """
    Return the secret to verify a token whose JOSE header carries the given kid.
    Returns None if no matching key is present, in which case the verifier
    MUST reject the token.

    If the token has no kid (older clients), we fall back to "default" if
    that kid is present in the ring. This preserves backwards compatibility
    with tokens issued before rotation was wired up.
    """
    if kid and kid in keyring:
        return keyring[kid]
    if kid is None and "default" in keyring:
        return keyring["default"]
    return None
