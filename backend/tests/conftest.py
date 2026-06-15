"""Pytest fixtures for merchant API smoke tests."""

import os
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient

_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db.name}"
os.environ["ENVIRONMENT"] = "development"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["STORAGE_ROOT"] = "/tmp/merchant-test-storage"

from app.db.models import ApiKey, get_session_factory  # noqa: E402
from app.domain.utils import now_iso  # noqa: E402
from app.lib.crypto import generate_api_key, hash_key  # noqa: E402
from app.main import app  # noqa: E402


def _seed_keys() -> tuple[str, str]:
    public_key = generate_api_key("pk")
    admin_key = generate_api_key("sk")
    factory = get_session_factory()
    db = factory()
    try:
        db.query(ApiKey).delete()
        db.add(
            ApiKey(
                id=str(uuid.uuid4()),
                key_hash=hash_key(public_key),
                key_prefix="pk_",
                role="public",
                created_at=now_iso(),
            )
        )
        db.add(
            ApiKey(
                id=str(uuid.uuid4()),
                key_hash=hash_key(admin_key),
                key_prefix="sk_",
                role="admin",
                created_at=now_iso(),
            )
        )
        db.commit()
    finally:
        db.close()
    return public_key, admin_key


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        public_key, admin_key = _seed_keys()
        c.public_key = public_key
        c.admin_key = admin_key
        yield c


@pytest.fixture
def public_headers(client):
    return {"Authorization": f"Bearer {client.public_key}"}


@pytest.fixture
def admin_headers(client):
    return {"Authorization": f"Bearer {client.admin_key}"}
