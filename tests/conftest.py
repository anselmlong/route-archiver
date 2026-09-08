"""Shared fixtures: an isolated SQLite DB per test, a Telegram initData
signer matching the real Mini App algorithm, and a FastAPI TestClient
wired to that isolated DB."""
import hashlib
import hmac
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

TEST_BOT_TOKEN = "123456:TEST-TOKEN-not-a-real-secret"


def sign_init_data(bot_token: str, user_id: int, first_name: str = "Tester",
                    auth_date: int | None = None) -> str:
    """Build a validly-signed Telegram WebApp initData string for tests."""
    params = {
        "user": f'{{"id":{user_id},"first_name":"{first_name}"}}',
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAtestquery",
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


@pytest.fixture
def storage_module():
    import storage as storage_mod
    return storage_mod


@pytest.fixture
def storage(tmp_path, storage_module):
    return storage_module.Storage(db_path=tmp_path / "test.db")


@pytest.fixture
def api_module(tmp_path, monkeypatch):
    """Import api.py with an isolated DB + fixed test bot token."""
    monkeypatch.setenv("BOT_TOKEN", TEST_BOT_TOKEN)
    import api as api_mod
    import storage as storage_mod

    api_mod.storage = storage_mod.Storage(db_path=tmp_path / "api_test.db")
    api_mod.BOT_TOKEN = TEST_BOT_TOKEN
    return api_mod


@pytest.fixture
def client(api_module):
    from fastapi.testclient import TestClient
    return TestClient(api_module.app)
