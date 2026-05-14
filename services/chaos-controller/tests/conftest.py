"""
Test fixtures for chaos-controller.

The chaos-controller is async + stateful. Tests use FastAPI's TestClient
which spins up the lifespan handler synchronously and lets us drive the
endpoints. For tests that need to inspect the mock cluster directly we
import it and instantiate it without going through FastAPI.
"""
import os
import sys

# Force mock mode for tests — never depend on a real cluster
os.environ["CHAOS_MODE"] = "mock"

# Allow `from app.X` imports from the tests directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    """A TestClient with the lifespan handler invoked.

    Use this for any test that exercises HTTP endpoints. The client must be
    used as a context manager to ensure startup/shutdown fire."""
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def mock_cluster():
    """A fresh MockCluster instance for direct-method testing.

    Avoids the FastAPI / asyncio.create_task indirection used by endpoints —
    lets us await action coroutines synchronously and observe state changes."""
    from app.cluster import MockCluster
    return MockCluster()
