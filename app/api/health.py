from fastapi import APIRouter, HTTPException
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.db import engine

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live():
    return {"status": "ok"}


@router.get("/ready")
def ready():
    try:
        with engine().connect() as connection:
            connection.execute(text("SELECT job_id FROM video_dispatches LIMIT 0"))
        with Redis.from_url(
            settings().redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2
        ) as redis:
            redis.ping()
    except (SQLAlchemyError, RedisError) as exc:
        raise HTTPException(503, "Database or broker unavailable") from exc
    return {"status": "ready"}

