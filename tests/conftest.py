import os

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    os.environ["DATABASE_PATH"] = str(db)
    init_db(str(db))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def make_client(client):
    """每个账号一个独立会话，避免共用 cookie。"""
    clients = []

    def _make(username: str, password: str = "123456") -> TestClient:
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        r = c.post("/api/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def login(client, username="admin", password="123456"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture()
def authed(make_client):
    return make_client("admin")


@pytest.fixture()
def reviewer(make_client):
    return make_client("reviewer")


def set_policy(client, policy="skip_duplicate", description="遇重复幂等键跳过并计数"):
    r = client.post(
        "/api/idempotency/policy",
        json={"policy": policy, "description": description},
    )
    assert r.status_code == 200, r.text
    return r.json()
