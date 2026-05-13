"""
conftest.py for api-gateway tests.

IMPORTANT: Environment variables must be set BEFORE importing any app module
because pydantic-settings reads them at class-definition time (when Settings()
is called at module level in config.py). Importing first then patching is too late.
"""
import os
import pytest

# Use a valid secret (≥32 chars, not in banned list) so Settings() doesn't raise
# on import. This must happen before any 'from app...' imports in test files.
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-definitely-long-enough-for-tests")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
