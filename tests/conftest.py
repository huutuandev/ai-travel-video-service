from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.db import engine
from app.main import create_app

TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
KEY_A = "tenant-a-test-key-" + "a" * 32
KEY_B = "tenant-b-test-key-" + "b" * 32


@pytest.fixture
def headers():
    return {"X-Service-API-Key": KEY_A}


@pytest.fixture
def payload():
    return {
        "requestId": "request-1",
        "productId": "tour-42",
        "tour": {"title": "Da Nang"},
        "images": ["https://cdn.example.com/tour.jpg"],
        "config": {"mode": "LOCAL", "durationSeconds": 30, "aspectRatio": "16:9"},
    }


@pytest.fixture
def configuration(monkeypatch):
    import json

    monkeypatch.setenv("SERVICE_API_KEYS", json.dumps({TENANT_A: KEY_A, TENANT_B: KEY_B}))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.setenv("MAX_REQUEST_BYTES", "262144")
    monkeypatch.setenv("MAX_JOB_ATTEMPTS", "3")
    monkeypatch.setenv("AI_DIRECTOR_ENABLED", "false")
    monkeypatch.setenv("TTS_ENABLED", "false")
    settings.cache_clear()
    yield
    if engine.cache_info().currsize:
        engine().dispose()
        engine.cache_clear()
    settings.cache_clear()


@pytest.fixture
def test_schema(configuration, monkeypatch):
    import os

    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("Set TEST_DATABASE_URL or use the Compose video-tests service")
    schema = f"video_test_{uuid4().hex}"
    admin = create_engine(database_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = make_url(database_url).update_query_dict({"options": f"-csearch_path={schema}"})
    monkeypatch.setenv("DATABASE_URL", scoped_url.render_as_string(hide_password=False))
    settings.cache_clear()
    try:
        yield
    finally:
        if engine.cache_info().currsize:
            engine().dispose()
            engine.cache_clear()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def migrated(test_schema):
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def client(configuration):
    with TestClient(create_app()) as http:
        yield http

