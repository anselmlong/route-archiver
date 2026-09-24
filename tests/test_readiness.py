"""Readiness must detect storage failure without changing the database."""
import sqlite3


def test_readiness_healthy(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["cache-control"] == "no-store"


def test_missing_database_is_not_recreated(client, api_module):
    path = api_module.storage._db_path
    path.unlink()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"ok": False}
    assert not path.exists()
    assert client.get("/healthz").status_code == 200


def test_missing_routes_schema_is_not_ready(client, api_module):
    with sqlite3.connect(api_module.storage._db_path) as conn:
        conn.execute("DROP TABLE routes")
    assert client.get("/readyz").status_code == 503


def test_readiness_does_not_expose_storage_details(client, api_module, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("private database path or secret")
    monkeypatch.setattr(api_module.storage, "check_readiness", broken)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert "private" not in response.text
