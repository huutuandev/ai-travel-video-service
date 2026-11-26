import pytest
from conftest import KEY_B

BASE = "/api/v1/videos"


def create(client, payload, headers):
    response = client.post(f"{BASE}/generate", json=payload, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()["jobId"]


@pytest.mark.parametrize("key", [None, "incorrect"])
def test_authentication(client, key):
    headers = {"X-Service-API-Key": key} if key else {}
    assert client.get(BASE, headers=headers).status_code == 401


@pytest.mark.parametrize("image_url", [
    "https://127.0.0.1/a.jpg",
    "https://[::1]/a.jpg",
    "https://metadata.internal/a.jpg",
    "https://user:secret@cdn.example.com/a.jpg",
    "http://cdn.example.com/a.jpg",
])
def test_media_validation(client, payload, headers, image_url):
    payload["images"] = [image_url]
    assert client.post(f"{BASE}/generate", json=payload, headers=headers).status_code == 422


def test_invalid_duration_and_untrusted_tenant(client, payload, headers):
    payload["config"]["durationSeconds"] = 1
    payload["tenantId"] = "attacker"
    response = client.post(f"{BASE}/generate", json=payload, headers=headers)
    assert response.status_code == 422
    assert len(response.json()["detail"]) == 2


def test_body_limit(client, headers):
    chunks = (b"x" * 100_000 for _ in range(3))
    assert client.post(f"{BASE}/generate", content=chunks, headers=headers).status_code == 413


def test_create_and_read(migrated, client, payload, headers):
    job_id = create(client, payload, headers)
    response = client.get(f"{BASE}/{job_id}", headers=headers)
    assert response.status_code == 200
    job = response.json()
    assert job["jobId"] == job_id
    assert job["status"] == "QUEUED"
    assert job["progress"] == 0
    assert job["actualCostUsd"] == "0.0000"
    assert job["outputUrl"] is None


def test_idempotency(migrated, client, payload, headers):
    job_id = create(client, payload, headers)
    assert create(client, payload, headers) == job_id
    payload["productId"] = "another"
    assert client.post(f"{BASE}/generate", json=payload, headers=headers).status_code == 409
    assert client.get(BASE, headers=headers).json()["total"] == 1


def test_tenant_isolation(migrated, client, payload, headers):
    other_headers = {"X-Service-API-Key": KEY_B}
    own_id = create(client, payload, headers)
    other_id = create(client, payload, other_headers)
    assert own_id != other_id
    assert client.get(f"{BASE}/{own_id}", headers=other_headers).status_code == 404
    page = client.get(f"{BASE}?pageSize=1", headers=other_headers).json()
    assert page["pageSize"] == page["total"] == 1
    assert [item["jobId"] for item in page["items"]] == [other_id]


def test_missing_job(migrated, client, headers):
    job_id = "33333333-3333-4333-8333-333333333333"
    assert client.get(f"{BASE}/{job_id}", headers=headers).status_code == 404


def test_liveness_and_docs(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200

