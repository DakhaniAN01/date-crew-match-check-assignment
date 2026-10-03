import os
import tempfile

# Configure before the app is imported.
_tmp = tempfile.mkdtemp(prefix="matchcheck-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
from cryptography.fernet import Fernet  # noqa: E402

os.environ["FIELD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["LOGIN_RATE_LIMIT_PER_MINUTE"] = "1000"
os.environ["RATE_LIMIT_PER_MINUTE"] = "10000"
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, rate_limiter, shortlist_cache  # noqa: E402
from app.seed import DEMO_PASSWORD  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_infra():
    rate_limiter.reset()
    shortlist_cache.clear()
    yield


def login(client, email):
    res = client.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()


@pytest.fixture(scope="session")
def tokens(client):
    return {
        name: login(client, f"{name}@{domain}")
        for name, domain in [("priya", "datecrew.test"), ("rahul", "datecrew.test"), ("meera", "datecrew.test"), ("sam", "northstar.test")]
    }


@pytest.fixture(scope="session")
def auth(tokens):
    return {name: {"Authorization": f"Bearer {t['access_token']}"} for name, t in tokens.items()}
