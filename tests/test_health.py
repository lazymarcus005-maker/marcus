from fastapi.testclient import TestClient

from harness.api.app import create_app


def test_healthz_ok():
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
