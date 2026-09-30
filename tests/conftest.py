import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{_tmp}/test.db", SEED_DEMO_DATA="true", RUN_SCHEDULER="false",
                  XAI_API_KEY="", SECRET_KEY="test-secret-key-that-is-long-enough-for-hs256")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def login(client, idn, pw="Demo@1234"):
    r = client.post("/api/auth/login", json={"id_number": idn, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def desk(client):
    return login(client, "STF003")


@pytest.fixture(scope="session")
def head(client):
    return login(client, "STF002")


@pytest.fixture(scope="session")
def cataloguer(client):
    return login(client, "STF004")


@pytest.fixture(scope="session")
def student(client):
    return login(client, "NITT/HND/24/001")
